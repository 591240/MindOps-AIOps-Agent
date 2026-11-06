"""诊断质量评测脚本（P0-1）。

用法::

    python scripts/eval_diagnosis.py                  # 只跑规则基线（零 LLM 成本）
    python scripts/eval_diagnosis.py --mode single    # 单轮 LLM（关闭回环）
    python scripts/eval_diagnosis.py --mode loop      # 完整回环
    python scripts/eval_diagnosis.py --all            # 三种模式对照（本项目最有价值的跑法）
    python scripts/eval_diagnosis.py --all --limit 8  # 先小样本试跑

设计说明：

- **不落库**。评测不创建 incident / step / loop_audit，只读模型配置与知识检索，
  因此可以随时重跑，不会污染业务数据。
- **为什么必须做三基线对照**：只跑"完整回环"只能得到一个数字，无法回答
  "回环到底有没有用"。必须同时跑「纯规则（`rule`）」「单轮 LLM（`single`，
  关闭回环）」「完整回环（`loop`）」三者，差值才是 F-04 回环的真实增量。
- `rule` 模式零成本，可反复跑；`single` / `loop` 会真实调用大模型，先用
  `--limit` 探一下再全量。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents import calibration, confidence_loop, loop_config, loop_strategies, model_router
from app.agents import context as context_agent
from app.agents import diagnose as diagnose_agent
from app.agents import triage
from app.db import close_db, init_db
from app.incidents.tokenizer import normalize_text

if hasattr(sys.stdout, "reconfigure"):
    try:  # Windows 控制台默认 GBK，中文输出需要切到 UTF-8
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 编码设置失败不影响评测
        pass

DATASET = Path(__file__).resolve().parent / "eval_dataset.yaml"
MODES = ("rule", "single", "loop")
DIFFICULTIES = ("easy", "middle", "hard")


# ---------------------------------------------------------------- 数据与判定

def load_dataset(path: Path) -> list[dict]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    samples = payload.get("samples") or []
    if not isinstance(samples, list) or not samples:
        raise SystemExit(f"评测集为空或格式错误：{path}")
    return samples


# 未定位标记：结论自称"没找到根因"时，即使碰巧提到了期望关键词，也算**未命中**。
# 不排除这类结论，评测会虚高 —— "无法定位，需补充下游依赖指标" 会因为含"下游"
# 被误判成命中了 M03 的期望关键词，而这显然不是一次成功的诊断。
UNRESOLVED_MARKERS = (
    "无法定位", "无法确定", "无法可靠", "无法判断", "信息不足", "不足以",
    "需补充", "需要补充", "仅能推测", "只能是推测", "暂无", "无法给出",
)


def judge_direction(sample: dict, root_cause: str | None) -> bool:
    """**方向命中**：只看结论方向对不对，不看它是否附带免责声明。

    这一档回答"模型有没有想对方向"，是模型能力的下界估计。
    """
    text = normalize_text(root_cause)
    if not text:
        return False
    keywords = sample.get("expect_keywords") or []
    return any(normalize_text(item) in text for item in keywords)


def judge(sample: dict, root_cause: str | None) -> bool:
    """**确定命中**：方向对 **且** 结论没有自称"未定位"。

    为什么要分两档：实测发现同一个诊断，回环前后**方向都正确**，差别只在
    回环后多了一句"但尚需补充 XX 证据方能确认"。若只报这一档，会把"表达更
    谨慎"误判成"诊断错了"；若只报方向档，又看不出结论可用性的差异。
    两档一起报，才能看出"回环降低了结论确定性"这个真实影响。
    """
    if not judge_direction(sample, root_cause):
        return False
    text = normalize_text(root_cause)
    return not any(normalize_text(marker) in text for marker in UNRESOLVED_MARKERS)


def build_state(sample: dict, snapshot: dict) -> dict:
    component = sample.get("component") or "unknown"
    severity = sample.get("severity") or "warning"
    return {
        "run_id": f"eval-{sample['id']}",
        "incident_id": 0,  # 评测不落库，incident_id 只作为占位
        "title": sample.get("title") or "",
        "alerts": [
            {
                "id": 0,
                "title": sample.get("title") or "",
                "detail": sample.get("detail") or "",
                "severity": severity,
                "source": "eval",
                "component": component,
            }
        ],
        "component": component,
        "severity": severity,
        "brief": "",
        "pattern_hit": False,
        "rule_root_cause": "",
        "context": {},
        "similar_cases": [],
        "kb_chunks": [],
        "root_cause": "",
        "root_cause_confidence": 0.0,
        "root_cause_source": "",
        "missing_info": [],
        "actions": [],
        "retry_count": 0,
        "needs_human": False,
        "human_reason": "",
        "status": "diagnosing",
        "model_role": "chat",
        "model_id": None,
        "loop_retrieval": None,
        **snapshot,
    }


# ------------------------------------------------------------------ 单例评测

async def score(state: dict, *, rule_hit: bool) -> dict:
    """对当前 state 的自报置信度做一次质量收口（与线上同一套逻辑）。"""
    scored = calibration.score_diagnosis(
        component=state.get("component"),
        root_cause=state.get("root_cause"),
        self_report=state.get("root_cause_confidence"),
        source=state.get("root_cause_source"),
        rule_hit=rule_hit,
        similar_cases=state.get("similar_cases"),
        kb_chunks=state.get("kb_chunks"),
        threshold=state.get("conf_threshold"),
    )
    state["root_cause_confidence"] = scored["confidence"]
    return scored


async def run_case(sample: dict, mode: str, snapshot: dict) -> dict:
    """跑一条样本；返回评测明细（不写任何业务表）。"""
    state = build_state(sample, snapshot)
    detail: dict = {
        "id": sample["id"],
        "difficulty": sample.get("difficulty") or "middle",
        "mode": mode,
        "component": state["component"],
        "expect_component": sample.get("expect_component"),
        "root_cause": "",
        "confidence": None,
        "self_report": None,
        "source": None,
        "signals": [],
        "retries": 0,
        "model_id": None,
        "error": None,
    }

    # ---- Triage：规则层优先 ----
    state.update(await triage.run(state))
    if state["pattern_hit"]:
        scored = await score(state, rule_hit=True)
        detail.update(
            root_cause=state["root_cause"],
            confidence=scored["confidence"],
            self_report=scored["self_report"],
            source="rule",
            signals=[],
        )
        detail["hit"] = judge(sample, detail["root_cause"])
        detail["hit_loose"] = judge_direction(sample, detail["root_cause"])
        return detail

    if mode == "rule":
        # 规则基线：未命中即算失败（这正是"纯规则"口径的意义）
        detail["hit"] = False
        return detail

    # ---- 诊断循环：single 关闭回环，loop 用快照里的 max_retry ----
    max_retry = 0 if mode == "single" else int(snapshot.get("max_retry", 2))
    model_id, role, _, _ = await model_router.pick_model("chat")
    state["model_id"], state["model_role"] = model_id, role

    scored: dict = {}
    while True:
        state.update(await context_agent.run(state))
        state.update(await diagnose_agent.run(state))
        scored = await score(state, rule_hit=False)

        if state["root_cause_confidence"] >= float(state["conf_threshold"]):
            break
        if state["retry_count"] >= max_retry:
            break

        # 回环边：复用线上策略与检索，但**不落 LoopAudit**
        state["retry_count"] += 1
        retry_index = int(state["retry_count"])
        requested = "reasoning" if (retry_index == 1 and state.get("reasoning_escalation")) else "chat"
        next_id, next_role, escalated, skip_reason = await model_router.pick_model(requested)
        strategy = loop_strategies.build_strategy(
            retry_index, bool(state.get("cross_component_fallback", True))
        )
        audit = await confidence_loop.run_retrieval(
            state,
            role=next_role,
            strategy=strategy,
            model_id=next_id,
            escalated=escalated,
            skip_reason=skip_reason,
        )
        state["loop_retrieval"] = {
            "query": audit["query_after"],
            "recall_top_k": audit["recall_top_k"],
            "sim_floor": audit["sim_floor"],
            "component_scope": audit["component_scope"],
        }
        state["model_role"], state["model_id"] = next_role, next_id

    detail.update(
        root_cause=state.get("root_cause") or "",
        confidence=scored.get("confidence"),
        self_report=scored.get("self_report"),
        source=state.get("root_cause_source"),
        signals=scored.get("signals") or [],
        retries=int(state["retry_count"]),
        model_id=state.get("model_id"),
    )
    detail["hit"] = judge(sample, detail["root_cause"])
    detail["hit_loose"] = judge_direction(sample, detail["root_cause"])
    return detail


async def run_mode(mode: str, samples: list[dict], snapshot: dict) -> list[dict]:
    results: list[dict] = []
    for sample in samples:
        try:
            results.append(await run_case(sample, mode, snapshot))
        except Exception as exc:  # noqa: BLE001 - 单条失败不该中断整轮评测
            results.append(
                {
                    "id": sample.get("id"), "difficulty": sample.get("difficulty") or "middle",
                    "mode": mode, "hit": False, "error": f"{type(exc).__name__}: {exc}",
                    "root_cause": "", "confidence": None, "self_report": None,
                    "source": None, "signals": [], "retries": 0, "model_id": None,
                }
            )
    return results


# -------------------------------------------------------------------- 报告

def _rate(hits: int, total: int) -> str:
    return "—" if total == 0 else f"{hits / total * 100:.1f}%"


def summarize(results: list[dict], difficulty: str | None = None, loose: bool = False) -> tuple[int, int]:
    rows = [r for r in results if difficulty is None or r["difficulty"] == difficulty]
    key = "hit_loose" if loose else "hit"
    return sum(1 for r in rows if r.get(key)), len(rows)


def print_report(mode_results: dict[str, list[dict]], verbose: bool) -> None:
    print("\n## 诊断评测结果\n")
    print(
        "> **确定命中**：方向正确 且 结论未自认\"未定位\"（结论可直接用于处置）。\n"
        "> **方向命中**：只看模型有没有想对方向，含\"需补充证据\"等谨慎免责表述。\n"
        "> 两档的差值 = 回环让结论更谨慎后，\"结论可用性\"的边际损失。"
    )

    header = (
        "| 模式 | 确定命中 | 确定率 | 方向命中 | 方向率 | "
        "easy(确/方) | middle(确/方) | hard(确/方) |"
    )
    print(header)
    print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for mode, results in mode_results.items():
        s_hit, s_total = summarize(results)
        l_hit, l_total = summarize(results, loose=True)
        cells = []
        for level in DIFFICULTIES:
            sh, st = summarize(results, level)
            lh, lt = summarize(results, level, loose=True)
            cells.append(f"{sh}/{st} · {lh}/{lt}")
        print(
            f"| `{mode}` | {s_hit}/{s_total} | {_rate(s_hit, s_total)} | "
            f"{l_hit}/{l_total} | {_rate(l_hit, l_total)} | " + " | ".join(cells) + " |"
        )

    if "loop" in mode_results and "single" in mode_results:
        loop_hit, loop_total = summarize(mode_results["loop"])
        single_hit, _ = summarize(mode_results["single"])
        loop_loose, _ = summarize(mode_results["loop"], loose=True)
        single_loose, _ = summarize(mode_results["single"], loose=True)
        strict_delta = (loop_hit - single_hit) / loop_total * 100 if loop_total else 0.0
        loose_delta = (loop_loose - single_loose) / loop_total * 100 if loop_total else 0.0
        print(
            "\n**回环增量（loop − single）**：\n"
            f"- 确定命中口径：{loop_hit - single_hit:+d} 条（{strict_delta:+.1f} 个百分点）\n"
            f"- 方向命中口径：{loop_loose - single_loose:+d} 条（{loose_delta:+.1f} 个百分点）\n"
            "> 若确定口径下滑、方向口径持平 —— 说明回环没有降低诊断方向，只是让结论更谨慎"
            "（F-04 回环的预期副作用），不构成质量倒退。"
        )
    elif len(mode_results) >= 2:
        modes = list(mode_results.keys())
        a, b = modes[0], modes[-1]
        a_hit, a_total = summarize(mode_results[a])
        b_hit, _ = summarize(mode_results[b])
        delta = (b_hit - a_hit) / a_total * 100 if a_total else 0.0
        print(f"\n**增量（{b} − {a}）**：{b_hit - a_hit:+d} 条（{delta:+.1f} 个百分点）。")

    # 模型使用情况：提醒"是不是在用启发式桩跑"
    for mode, results in mode_results.items():
        if mode == "rule":
            continue
        # 只统计**走到了 LLM 路径**的样本：规则命中短路的不算，否则会误报
        llm_cases = [r for r in results if r.get("source") != "rule" and not r.get("error")]
        if llm_cases and not any(r.get("model_id") for r in llm_cases):
            print(
                f"\n> ⚠️ `{mode}` 模式有 {len(llm_cases)} 条走到了模型路径，但都没拿到可用模型"
                "（`model_id` 为空），本轮实际走的是启发式桩 —— 结果只反映兜底逻辑，"
                "不代表真实模型能力。请先在「模型设置」里启用一个 chat 角色模型再重跑。"
            )
            break

    if verbose:
        print("\n### 明细\n")
        for mode, results in mode_results.items():
            print(f"\n**{mode}**\n")
            for r in results:
                strict = bool(r.get("hit"))
                loose = bool(r.get("hit_loose"))
                mark = "✅" if strict else ("🟡" if loose else "❌")
                note = f" | {r['error']}" if r.get("error") else ""
                tag = " ⚠️方向对但结论未定位" if (loose and not strict) else ""
                print(
                    f"- {mark} `{r['id']}` [{r['difficulty']}] "
                    f"src={r.get('source')} conf={r.get('confidence')} "
                    f"retry={r.get('retries')} signals={r.get('signals') or '[]'} "
                    f"→ {(r.get('root_cause') or '（无结论）')[:60]}{tag}{note}"
                )
            # 每条模式末尾给一句失败模式统计，便于看"哪类错得多"
            signal_counts: dict[str, int] = {}
            for r in results:
                for signal in r.get("signals") or []:
                    signal_counts[signal] = signal_counts.get(signal, 0) + 1
            if signal_counts:
                summary = "、".join(f"{k}×{v}" for k, v in sorted(signal_counts.items()))
                print(f"\n  失败模式信号：{summary}")


# ---------------------------------------------------------------------- 入口

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="智维诊断质量评测（P0-1）")
    parser.add_argument(
        "--mode", action="append", choices=list(MODES),
        help="评测模式，可重复指定；默认 rule（零 LLM 成本）",
    )
    parser.add_argument("--all", action="store_true", help="等价于 --mode rule --mode single --mode loop")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（先探成本）")
    parser.add_argument(
        "--difficulty", action="append", choices=list(DIFFICULTIES),
        help="只跑指定难度档，可重复；如 `--difficulty middle --difficulty hard` 只看 LLM 能力",
    )
    parser.add_argument("--dataset", type=Path, default=DATASET, help="评测集 YAML 路径")
    parser.add_argument("--json", type=Path, default=None, help="把逐条明细导出为 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印逐条明细与失败模式统计")
    return parser.parse_args()


async def amain() -> int:
    args = parse_args()
    samples = load_dataset(args.dataset)
    if args.difficulty:
        wanted = set(args.difficulty)
        samples = [s for s in samples if (s.get("difficulty") or "middle") in wanted]
    if args.limit and args.limit > 0:
        samples = samples[: args.limit]
    if not samples:
        raise SystemExit("过滤后没有样本，请检查 --difficulty / --limit")
    modes = list(MODES) if args.all else (args.mode or ["rule"])

    await init_db()
    try:
        snapshot = await loop_config.snapshot()
        print(
            f"评测集：{args.dataset.name} ｜ 样本 {len(samples)} 条 ｜ "
            f"模式 {', '.join(modes)} ｜ "
            f"阈值 conf_threshold={snapshot.get('conf_threshold')} max_retry={snapshot.get('max_retry')}"
        )
        mode_results: dict[str, list[dict]] = {}
        for mode in modes:
            print(f"→ 正在跑 `{mode}` …")
            mode_results[mode] = await run_mode(mode, samples, snapshot)
        print_report(mode_results, args.verbose)

        if args.json:
            args.json.write_text(
                json.dumps(mode_results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n明细已导出：{args.json}")
    finally:
        await close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
