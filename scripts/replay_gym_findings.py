"""Rejudge captured attacks against complete local briefs, with optional coaching.

This isolates filtering and presentation; it does not rerun retrieval or measure
recall of newly generated attacks. Outputs contain private brief content.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django

django.setup()
from apps.ai.benchmark_capture import CaptureCalls
from apps.ai.prompt_catalog import render_prompt
from apps.argument_gym import pipeline, ingestion


def section(text, start, end):
    return json.loads(text.split(start, 1)[1].split(end, 1)[0].strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-dir', action='append', required=True, help='Directory with result.json and call-*.json from benchmark_real_briefs.py')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--model', default='gpt-5.5')
    parser.add_argument('--coach', action='store_true')
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    if (ROOT / 'cle_real_briefs').resolve() not in output.parents:
        parser.error('Output must be inside ignored cle_real_briefs')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    def file_prompt(key, **kwargs):
        return render_prompt(key, allow_database_override=False, **kwargs)
    reports = []
    for index, source in enumerate(args.capture_dir, 1):
        directory = Path(source)
        baseline = json.loads((directory / 'result.json').read_text())
        calls = [json.loads(p.read_text()) for p in sorted(directory.glob('call-*.json'))]
        judge = next(c for c in calls if 'neutral judge' in str(c.get('messages', '')))
        text = judge['messages'][1]['content']
        sources = section(text, 'Retrieved legal sources, each with a source id:', 'Proposed attacks:')
        arguments = section(text, 'Argument map of the brief:', 'Passages of the brief, with unit ids:')
        attacks = section(text, 'Proposed attacks:', 'Return JSON')
        path = ROOT / baseline['path']
        content = path.read_bytes()
        ingested = ingestion.ingest_upload(content, filename=path.name,
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document', split_exhibits=False)
        units = ingested['metadata']['units']
        dest = output / f'document-{index:02}'
        dest.mkdir()
        print(f'Starting document {index}: {len(units)} units', flush=True)
        with patch.object(pipeline, 'ai_enabled', return_value=True), \
             patch.object(pipeline, 'render_prompt', side_effect=file_prompt), \
             CaptureCalls(dest, live=True, model=args.model, reasoning='medium') as capture:
            assessments, trace = pipeline.judge_stage(units, arguments, attacks, sources, jurisdiction='Ohio')
            kept = pipeline.select_challenges(attacks, assessments)
            responses, coach_trace = [], None
            if args.coach and kept:
                by_id = {a['attackId']: a for a in assessments}
                units_by_id = {u['id']: u for u in units}
                coach_input = [{**a, 'attackId': a['id'], 'finding': by_id[a['id']]['finding'],
                    'reviewAction': by_id[a['id']]['reviewAction'],
                    'judgeAssessment': by_id[a['id']]['assessment'],
                    'target': pipeline._target_for(units_by_id[a['unitId']], units_by_id)} for a in kept]
                responses, coach_trace = pipeline.coach_stage(units, coach_input, sources, [], jurisdiction='Ohio')
        complete = (trace['method'] == 'llm' and all(c['status'] == 'complete' for c in capture.calls)
                    and (coach_trace is None or coach_trace['method'] == 'llm'))
        report = dict(input=baseline['path'], sha256=hashlib.sha256(content).hexdigest(),
            capture_dir=str(directory), original_findings=len(baseline['challenges']), units=len(units),
            assessments=assessments, kept=kept, responses=responses, trace=trace, coach_trace=coach_trace,
            complete=complete, limitation='Captured candidates and sources; no new retrieval or recall measurement.')
        reports.append(report)
        (dest / 'result.json').write_text(json.dumps(report, indent=2))
        (output / 'report.json').write_text(json.dumps(reports, indent=2))
        print(f'Finished document {index}: {len(kept)} kept; complete={complete}', flush=True)
    if not all(r['complete'] for r in reports):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
