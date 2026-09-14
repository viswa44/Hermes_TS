"""Run with python -m data_cleaning_agent.main, or python main.py in this folder."""

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_cleaning_agent.agent.cleaning_agent import DataCleaningAgent
from data_cleaning_agent.config.settings import Settings
from data_cleaning_agent.tools.s3_tool import publish_run, validate_bucket_name
from market_calendar.run_guarded import check_component


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Clean option observations into two validated Parquet tables.')
    parser.add_argument('input', type=Path, help='Local CSV / JSON / JSONL / Parquet file')
    parser.add_argument('--output-dir', type=Path, default=Path('output'))
    parser.add_argument('--planner', choices=['mistral', 'deterministic'], default='mistral')
    parser.add_argument('--upload', action='store_true', help='Publish a passing run to an existing S3 bucket')
    parser.add_argument('--bucket', help='Override S3_BUCKET')
    args = parser.parse_args(argv)
    try:
        market = check_component('cleaner', enforce_time=False)
        if not market['allowed']:
            print(json.dumps({'status': 'CALENDAR_UNAVAILABLE' if market['status'] == 'UNKNOWN' else 'SKIPPED_MARKET_HOLIDAY', 'gateway': market}))
            return 2 if market['status'] == 'UNKNOWN' else 0
        settings = Settings()
        bucket = args.bucket or settings.s3_bucket
        if args.upload:
            validate_bucket_name(bucket)
        result = DataCleaningAgent(settings, planner=args.planner).run(args.input, args.output_dir)
        summary = {
            'status': 'PASS' if result.passed else 'FAIL', 'run_dir': str(result.run_dir),
            'accepted_rows': result.accepted_rows, 'quarantined_rows': result.quarantined_rows,
        }
        if args.upload and result.passed:
            try:
                summary['s3'] = publish_run(result.run_dir, bucket, settings.s3_prefix, region=settings.aws_region)
            except Exception as exc:
                # Provider response bodies may contain sensitive information.
                summary['upload_status'] = 'FAILED'
                summary['upload_error_type'] = type(exc).__name__
                print(json.dumps(summary, indent=2))
                return 3
        print(json.dumps(summary, indent=2))
        return 0 if result.passed else 2
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error_type': type(exc).__name__, 'error': 'Check input path and configuration; see README.'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
