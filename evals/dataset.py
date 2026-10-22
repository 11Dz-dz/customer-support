"""测试集加载、约束与源数据参考标签。"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KINDS = {"catalog", "product", "order", "retrieval", "budget_search", "quote", "denial", "unbound",
         "refund", "policy", "clarify", "invalid_or_clarify", "promotion", "tech"}
GOLD_RULES = {
    "mountain_100": ("prefix", "Mountain-100"), "road_150": ("prefix", "Road-150"),
    "bottle_30": ("id", 870), "helmet": ("prefix", "Sport-100 Helmet"),
    "long_distance_bar": ("id", 947), "fatigue_bar": ("id", 946),
    "self_sealing_tube": ("id", 921), "road_tube": ("id", 922),
    "touring_tube": ("id", 923), "touring_3000": ("prefix", "Touring-3000"),
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_cases(path=None):
    path = Path(path) if path else ROOT / "cases.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids, families = set(), {}
    for case in cases:
        if case["id"] in ids:
            raise ValueError("重复 case_id: " + case["id"])
        ids.add(case["id"])
        if case["split"] not in {"dev", "holdout"} or case["actor"] not in {"owner", "other", "guest"}:
            raise ValueError("非法数据集划分或身份")
        if case["family"] in families and families[case["family"]] != case["split"]:
            raise ValueError("同一题目家族不能跨开发集和验收集")
        families[case["family"]] = case["split"]
        if not case["turns"] or case["expected_refunds"] not in {0, 1}:
            raise ValueError("非法轮次或退款期望")
        for turn in case["turns"]:
            if not turn["input"] or not set(turn["routes"]) <= {"sales", "billing", "tech_support"}:
                raise ValueError("非法问题或路由")
            if not 0 <= turn["max_executions"] <= 6:
                raise ValueError("非法工具预算")
            for check in turn["checks"]:
                if check["kind"] not in KINDS:
                    raise ValueError("未知评分器: " + check["kind"])
                if check["kind"] == "retrieval" and check["label"] not in GOLD_RULES:
                    raise ValueError("缺少参考标签规则")
    return cases


def prepare_gold(destination=None):
    from postgres_config import connect_database
    destination = Path(destination) if destination else ROOT / "retrieval_gold.json"
    with connect_database() as conn:
        rows = conn.execute("SELECT product_id,name,product_number,list_price,description_en,description_zh "
                            "FROM product_catalog WHERE list_price>0 ORDER BY product_id").fetchall()
    labels = {}
    for label, (kind, value) in GOLD_RULES.items():
        selected = [r for r in rows if (r["product_id"] == value if kind == "id" else r["name"].startswith(value))]
        if not selected or any(not (r["description_en"] or r["description_zh"]) for r in selected):
            raise ValueError("参考商品不存在或没有描述: " + label)
        labels[label] = {"product_ids": [r["product_id"] for r in selected], "source_evidence": selected}
    document = {"version": "1.0.0", "annotation_status": "source_rule_draft_requires_independent_review",
                "reference_set_complete": False,
                "policy": "固定人工规则选择名称/描述可支持的已知正例，不使用待测检索器或模型生成标签；非完整相关性标注。",
                "labels": labels}
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return document


def load_gold():
    path = ROOT / "retrieval_gold.json"
    if not path.exists():
        raise ValueError("请先执行 python run_evaluation.py --prepare-gold")
    gold = json.loads(path.read_text(encoding="utf-8"))
    for label in GOLD_RULES:
        if not gold["labels"].get(label, {}).get("product_ids"):
            raise ValueError("缺少参考正例: " + label)
    return gold
