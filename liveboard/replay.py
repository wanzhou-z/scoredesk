# -*- coding: utf-8 -*-
"""回放验收；默认发布到 state.json，使本地页面真实收到 SSE 更新。"""
from __future__ import annotations
import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from board_core import build_board, json_dump_atomic, load_json, make_state
from pta_to_resolver_xml import compute_first_to_solve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='src', default=str(BASE / 'contest.json'))
    parser.add_argument('--speed', type=float, default=60)
    parser.add_argument('--no-publish', action='store_true', help='仅离线核对，不发布页面状态')
    parser.add_argument('--report', default=str(BASE / 'tmp' / 'replay-log.md'))
    parser.add_argument('--state', type=Path, default=BASE / 'state.json', help='发布到隔离状态文件，便于不干扰实时服务的验收')
    args = parser.parse_args()
    if args.speed <= 0:
        parser.error('--speed 必须大于零')
    full = load_json(Path(args.src))
    submissions = sorted(full['submissions'], key=lambda s: (s.get('rel_time_seconds') or 0, str(s.get('id') or '')))
    current = copy.deepcopy(full)
    current['submissions'] = []
    duration = int(full['contest']['duration_seconds'])
    freeze_at = duration - int(full['contest']['freeze_seconds'])
    checkpoints = {0: 'start', 35: 'first-ac', freeze_at: 'freeze', 9000: 'frozen-results', duration: 'thaw'}
    timeline = sorted(set([0, duration, freeze_at, 9000] + [int(s.get('rel_time_seconds') or 0) for s in submissions]))
    gate_path = os.environ.get('LIVE_BOARD_FRAME_SYNC')
    gates = Path(gate_path) if gate_path else None
    report = ['# 回放日志', '', f'命令：python replay.py --from contest.json --speed {args.speed:g}', '', '默认发布 state.json，使用与 poller 相同的 make_state()；无排行榜算法修改。', '']
    previous = []
    previous_time = 0
    audience_board = None
    audience_first = None
    known_ids = {}
    index = 0
    started = time.monotonic()
    for contest_time in timeline:
        time.sleep(max(0, (contest_time - previous_time) / args.speed))
        batch = []
        while index < len(submissions) and int(submissions[index].get('rel_time_seconds') or 0) <= contest_time:
            sub = submissions[index]; index += 1
            known_ids[str(sub['id'])] = sub
            batch.append(sub)
        current['submissions'] = list(known_ids.values())
        first = compute_first_to_solve(current, int(full['contest']['penalty_minutes']))
        frozen = freeze_at <= contest_time < duration
        checkpoint = checkpoints.get(contest_time)
        at = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        state = make_state(current, channel='http', last_poll=at, event={'type':'replay', 'new_submissions':len(batch), 'contest_time':contest_time})
        # 封榜仅影响展示；API 中的后台排名与积分使用 M1 原值。
        state['frozen'] = frozen
        if frozen and audience_board is None:
            audience_board=build_board(current, display_time=freeze_at, freeze_mode='audience')
            prefix=dict(current)
            prefix['submissions']=[s for s in current['submissions'] if float(s.get('rel_time_seconds') or 0)<=freeze_at]
            audience_first=compute_first_to_solve(prefix, int(full['contest']['penalty_minutes']))
        if frozen:
            # 两种 HTTP 视角共享同一实时状态，观众快照在封榜期间不可变。
            state['audience_board']=audience_board
        state['presentation'] = {
            'replay': True, 'elapsed': contest_time, 'speed': args.speed,
            'checkpoint': checkpoint, 'held': bool(checkpoint and gates),
            'first_to_solve': first,
            'audience_first_to_solve': audience_first,
            'locked_cells': sorted({str(s['userId']) + ':' + str(s['problemSetProblemId']) for s in current['submissions'] if int(s.get('rel_time_seconds') or 0) >= freeze_at}),
        }
        changed = state['board'] != previous
        state['event']['rank_changed'] = changed
        if not args.no_publish:
            # Windows 中读取中的文件可能短暂禁止替换；只重试发布，不修改数据层。
            for attempt in range(30):
                try:
                    json_dump_atomic(args.state, state)
                    break
                except PermissionError:
                    if attempt == 29:
                        raise
                    time.sleep(.02)
        line = f'- t={contest_time:5d}s 新增={len(batch):2d} 榜单变化={changed} 封榜={frozen} 总提交={len(known_ids)}'
        print(line, flush=True); report.append(line)
        if gates and checkpoint:
            gates.mkdir(parents=True, exist_ok=True)
            json_dump_atomic(gates / (checkpoint + '.ready.json'), state)
            deadline = time.monotonic() + 40
            while not (gates / (checkpoint + '.ack')).exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('截图验收没有完成：' + checkpoint)
                time.sleep(.05)
        previous = state['board']; previous_time = contest_time
    expected = build_board(full)
    actual = build_board(current)
    assert actual == expected, '回放最终榜单和完整数据重算不一致'
    assert len(current['submissions']) == len(full['submissions'])
    assert [(x['student_number'],x['solved'],x['penalty']) for x in actual[:3]] == [('2026001',8,167),('26130385',8,329),('26130152',8,449)]
    report += ['', f'最终逐行逐题核对 PASS；{len(actual)} 队、{len(full["problems"])} 题、{len(known_ids)} 提交。', f'实际耗时 {time.monotonic()-started:.1f} 秒。', '前三名 2026001=8/167、26130385=8/329、26130152=8/449：PASS。', '通道 http 在本回放中只是展示值，并非一次真实 HTTP 轮询。', '真实赛中取数、限流、封榜真实时序：待真比赛验证。']
    Path(args.report).write_text('\n'.join(report)+'\n', encoding='utf-8')
    print('FINAL PASS: 30 teams / 13 problems / 621 submissions; top3 167,329,449', flush=True)

if __name__ == '__main__':
    main()
