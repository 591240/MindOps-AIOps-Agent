"""运行线上检索接口的 MRR@10、Hit@5、Recall@10 评测。"""
from __future__ import annotations
import argparse, json
import httpx

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--base", required=True); parser.add_argument("--token", required=True)
    parser.add_argument("--set", default="data/eval/golden_set.json"); args = parser.parse_args()
    cases = json.load(open(args.set, encoding="utf-8")); headers = {"Authorization": f"Bearer {args.token}"}
    rr, hit, recall = [], [], []
    with httpx.Client(base_url=args.base, headers=headers, timeout=30) as client:
        for case in cases:
            response = client.post("/api/v1/knowledge/search", json={"query": case["query"], "component": case.get("component"), "top_k": 10})
            response.raise_for_status(); results = response.json().get("results", []); expected = {str(item) for item in case.get("expected", [])}
            fields = ("source_doc", "doc_id", "chunk_id") if case.get("expected_docs") else ("component",)
            ranks = [i + 1 for i, item in enumerate(results) if any(str(item.get(field)) in expected for field in fields)]
            recalled = {str(item.get(field)) for item in results[:10] for field in fields}
            rr.append(1 / ranks[0] if ranks else 0); hit.append(any(rank <= 5 for rank in ranks)); recall.append(len(recalled & expected) / max(1, len(expected)))
    print(json.dumps({"MRR@10": sum(rr)/len(rr), "Hit@5": sum(hit)/len(hit), "Recall@10": sum(recall)/len(recall)}, ensure_ascii=False, indent=2)); return 0
if __name__ == "__main__": raise SystemExit(main())
