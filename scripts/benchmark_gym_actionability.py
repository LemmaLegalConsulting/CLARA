"""Live, closed-world controls for material findings; never a score for a brief."""
import argparse
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
import yaml

django.setup()
from apps.ai.benchmark_capture import CaptureCalls
from apps.ai.prompt_catalog import render_prompt
from apps.argument_gym import pipeline, ingestion


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--model', default='gpt-5.5')
    args = parser.parse_args()
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    suite = yaml.safe_load((ROOT / 'content/argument-gym/actionability.yaml').read_text())
    results = []
    def file_prompt(key, **kwargs):
        return render_prompt(key, allow_database_override=False, **kwargs)
    for case in suite['cases']:
        dest = directory / case['id']
        dest.mkdir()
        units = ingestion.structure_units(ingestion._plain_paragraphs(case['brief']))
        attacks = [dict(a, unitId=units[0]['id'], legalSourceIds=['rule'], recordMaterialIds=[])
                   for a in case['attacks']]
        sources = [{'id': 'rule', 'snippet': suite['rule'], 'title': 'Invented benchmark rule'}]
        with patch.object(pipeline, 'ai_enabled', return_value=True), \
             patch.object(pipeline, 'render_prompt', side_effect=file_prompt), \
             CaptureCalls(dest, live=True, model=args.model, reasoning='medium') as capture:
            assessments, trace = pipeline.judge_stage(units, [], attacks, sources,
                                                     jurisdiction='Fictional closed-world benchmark')
        complete = trace['method'] == 'llm' and bool(capture.calls) and all(c['status'] == 'complete' for c in capture.calls)
        kept = pipeline.select_challenges(attacks, assessments)
        actual = sorted(a['id'] for a in kept)
        result = dict(id=case['id'], status=('pass' if actual == sorted(case['expected_kept']) else 'fail') if complete else 'not_run',
                      expected=case['expected_kept'], actual=actual, assessments=assessments, trace=trace)
        results.append(result)
        (directory / 'report.json').write_text(json.dumps(results, indent=2))
        print(case['id'], result['status'], actual, flush=True)
    if any(r['status'] != 'pass' for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
