"""Workspace-only audition mix from actual native timeline values, no draft writes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from jianying_adapter.action_audio import render_candidate_audio as render, measure_balance, tempo_filters


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    for name in ('plan','draft','ffmpeg','output','report'): parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    read=lambda p: json.loads(Path(p).read_text('utf-8-sig'))
    report=render(read(args.plan),read(args.draft),args.ffmpeg,args.output,plan_path=args.plan)
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
    sys.exit(0 if report['ok'] else 1)
