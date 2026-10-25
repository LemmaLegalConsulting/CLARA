"""Short-lived, resource-limited document parsing worker (Linux production)."""
import base64
import json
import os
import signal
import fcntl
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

_SLOTS = threading.BoundedSemaphore(2)
MAX_RESULT_BYTES = 32 * 1024 * 1024


def run_extraction(content, *, filename, content_type, extractor):
    return _run_payload({'operation': 'extract', 'content': base64.b64encode(content).decode(),
                         'filename': filename, 'content_type': content_type, 'extractor': extractor})


class WorkerBusyError(RuntimeError):
    """Temporary capacity exhaustion; safe to retry, not a rejected template."""


def run_fill(content, state, *, preview=False):
    if len(content) > 15 * 1024 * 1024:
        raise ValueError("Template exceeds 15 MB input limit")
    result = _run_payload({'operation': 'fill', 'content': base64.b64encode(content).decode(),
                           'state': state, 'preview': preview})
    content = base64.b64decode(result['content'])
    return (content, result['marked'], result['assumed']) if preview else content


def render_text(source, values):
    from jinja2.sandbox import SecurityError
    try:
        return _run_payload({'operation': 'render', 'source': source, 'values': values})['text']
    except WorkerBusyError:
        raise
    except (TypeError, ValueError, OSError) as exc:
        raise SecurityError('Template could not be rendered within its safety and resource limits') from exc


def _run_payload(data):
    if not _SLOTS.acquire(blocking=False):
        raise WorkerBusyError("Document processing is busy; retry shortly")
    try:
        payload = json.dumps(data).encode()
        if len(payload) > 22 * 1024 * 1024:
            raise ValueError('Worker input exceeds size limit')
        with tempfile.TemporaryDirectory(prefix='clara-parser-') as directory:
            output = Path(directory) / 'result.json'
            with output.open('wb') as result:
                with _host_slot():
                    process = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), '--worker'],
                        stdin=subprocess.PIPE, stdout=result, stderr=subprocess.DEVNULL,
                        cwd=directory, start_new_session=True,
                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                             "LANG": "C.UTF-8", "TMPDIR": directory},
                    )
                    try:
                        process.communicate(input=payload, timeout=25)
                    finally:
                        # Kill the whole parser tree, including tools a backend spawned.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()

            if process.returncode or output.stat().st_size > MAX_RESULT_BYTES:
                raise ValueError("Document parser failed or exceeded its resource budget")
            data = json.loads(output.read_bytes())
            if 'error' in data:
                raise ValueError(data['error'])
            return data
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Document parsing exceeded its time limit") from exc
    finally:
        _SLOTS.release()


@contextmanager
def _host_slot():
    directory = Path(tempfile.gettempdir()) / f"clara-parser-slots-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("Unsafe parser lock directory")
    descriptor = None
    for index in range(2):
        fd = os.open(directory / str(index), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            descriptor = fd
            break
        except BlockingIOError:
            os.close(fd)
    if descriptor is None:
        raise WorkerBusyError("Document processing is busy; retry shortly")
    try:
        yield
    finally:
        os.close(descriptor)


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        payload = json.loads(sys.stdin.buffer.read(22 * 1024 * 1024))
        operation = payload.pop('operation')
        if operation == 'render':
            from apps.templates_app.jinja_filters import template_environment
            from apps.templates_app.field_values import template_field_values
            values = payload['values']
            if 'fields' in values:
                values['fields'] = template_field_values(values['fields'])
            result = {'text': template_environment().from_string(payload['source']).render(values)}
        elif operation == 'fill':
            from apps.exporting.template_fill import _render_fill_in_worker
            content = base64.b64decode(payload['content'], validate=True)
            output = _render_fill_in_worker(content, payload['state'], preview=payload['preview'])
            if payload['preview']:
                content, marked, assumed = output
                result = {'content': base64.b64encode(content).decode(), 'marked': marked, 'assumed': assumed}
            else:
                result = {'content': base64.b64encode(output).decode()}
        elif operation == 'extract':
            from apps.sources.document_text import _extract_in_worker
            payload['content'] = base64.b64decode(payload['content'], validate=True)
            result = _extract_in_worker(**payload)
        else:
            raise ValueError('Unsupported worker operation')
    except ValueError as exc:
        # Field-resolution errors contain instructions the fill UI must preserve.
        result = {'error': str(exc)[:500]}
    except Exception:
        result = {'error': 'Document could not be processed within the supported format and resource limits'}
    sys.stdout.write(json.dumps(result))


if __name__ == '__main__':
    main()
