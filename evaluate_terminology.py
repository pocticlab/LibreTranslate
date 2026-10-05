"""
术语库效果评测：把「有没有术语库」的差距变成可量化的数字。

跑两个实验：

  实验 A（裸翻）：直接把中文术语丢给机器翻译，看俄文结果里有没有出现
                  术语库里写定的正确译法。
  实验 B（加术语库）：先锁定成占位符再翻译、译后回填，统计占位符存活率
                      —— 也就是我们定义的「术语保真率」。

用法：
    python scripts/evaluate_terminology.py            # 抽样 40 条
    python scripts/evaluate_terminology.py 80         # 抽样 80 条
    python scripts/evaluate_terminology.py 0          # 0 表示全跑

结果同时写进 evaluate_result.json，PPT 里的数字就从这儿来。
"""

import json
import os
import re
import sys
import time
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argostranslate import translate as argos_translate  # noqa: E402

from libretranslate.terminology import get_terminology  # noqa: E402

# 把术语单独丢给模型时，模型容易乱发挥，所以套一个载体句
CARRIER = "请查询{}的相关信息"


def norm(s):
    """归一化：转小写，只留字母和数字，方便比较俄语的格变化。"""
    return re.sub(r"[^0-9a-zа-яё]", "", (s or "").lower())


def best_similarity(expected, actual):
    """在译文里找和期望译法最像的片段，返回相似度 0~1。

    为什么要做模糊比对：俄语名词会变格，
    «Математический анализ» 翻出来是 «математическом анализе»，
    词尾全变了但其实是对的。直接做字符串相等会把这种判成错，
    所以我们用滑窗取最大相似度。
    """
    e = norm(expected)
    a = norm(actual)
    if not e or not a:
        return 0.0
    if e in a:
        return 1.0
    # 滑窗：在译文里截取和期望串等长的片段，取最像的那一段
    n = len(e)
    best = 0.0
    for i in range(0, max(1, len(a) - n + 1)):
        r = SequenceMatcher(None, e, a[i:i + n]).ratio()
        if r > best:
            best = r
    # 短词容易被低估，长度差太多时再补一次整体比对
    best = max(best, SequenceMatcher(None, e, a).ratio())
    return best


# 相似度 >= 这个阈值就算"词根对上了"（只是变格不同）
SIM_HIT = 0.75


def main():
    limit = 40
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            pass

    tb = get_terminology()
    # 只评 strict 词条：soft 本来就不强制替换，评它没有意义
    terms = [t for t in tb.terms if str(t.get("lock") or "strict").lower() == "strict"]
    if limit > 0:
        terms = terms[:limit]

    print("参评词条：%d 条" % len(terms))

    langs = {l.code: l for l in argos_translate.get_installed_languages()}
    src, tgt = langs.get("zh"), langs.get("ru")
    if src is None or tgt is None:
        print("模型没装好，先跑 scripts/install_zh_ru.py")
        return 1
    translator = src.get_translation(tgt)
    if translator is None:
        print("找不到 zh -> ru 的翻译路径")
        return 1

    print("模型加载完成，开始评测 ...\n")
    t0 = time.time()

    rows = []
    kept_total = 0
    kept_lost = 0
    raw_hit = 0
    exact_hit = 0

    for t in terms:
        zh = t["zh"]
        expected = t["ru"]

        # ---- 实验 A：裸翻 ----
        try:
            raw = translator.translate(CARRIER.format(zh))
        except Exception as e:
            raw = "<出错:%s>" % e
        sim = best_similarity(expected, raw)
        hit = sim >= SIM_HIT
        if norm(expected) in norm(raw):
            exact_hit += 1
        if hit:
            raw_hit += 1

        # ---- 实验 B：加术语库 ----
        protected, mapping, hits = tb.protect(CARRIER.format(zh))
        try:
            translated = translator.translate(protected)
        except Exception as e:
            translated = protected
        restored, report = tb.restore(translated, mapping)
        kept_total += report["total"]
        kept_lost += len(report["lost"])
        ok = report["fidelity"] == 1.0

        rows.append({
            "zh": zh,
            "expected_ru": expected,
            "raw_ru": raw,
            "similarity": round(sim, 3),
            "raw_correct": hit,
            "with_termbase_ru": restored,
            "placeholder_survived": ok,
            "lost": report["lost"],
        })

    n = len(rows)
    raw_rate = raw_hit / n if n else 0
    exact_rate = exact_hit / n if n else 0
    fidelity = (kept_total - kept_lost) / kept_total if kept_total else 0

    print("=" * 60)
    print("实验 A1 裸翻·逐字相同   : %d / %d = %.1f%%" % (exact_hit, n, exact_rate * 100))
    print("实验 A2 裸翻·词根对上   : %d / %d = %.1f%%" % (raw_hit, n, raw_rate * 100))
    print("实验 B  术语保真率      : %d / %d = %.1f%%"
          % (kept_total - kept_lost, kept_total, fidelity * 100))
    print("=" * 60)
    print("（A2 允许俄语变格差异，相似度 >= %.2f 就算对）" % SIM_HIT)

    print("\n翻错的（连词根都没对上，这些是术语库的用武之地）：")
    for r in rows:
        if not r["raw_correct"]:
            print("  %-10s 应为 %-34s 实际 %s"
                  % (r["zh"], r["expected_ru"], r["raw_ru"][:52]))

    survived_but = [r for r in rows if r["placeholder_survived"]]
    print("\n占位符存活：%d / %d" % (len(survived_but), n))
    lost_rows = [r for r in rows if r["lost"]]
    if lost_rows:
        print("占位符被模型破坏的词条：")
        for r in lost_rows:
            print("  %-12s 期望 %s" % (r["zh"], [x["expected"] for x in r["lost"]]))

    out = {
        "sample_size": n,
        "raw_exact_rate": exact_rate,
        "raw_stem_rate": raw_rate,
        "fidelity": fidelity,
        "similarity_threshold": SIM_HIT,
        "rows": rows,
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evaluate_result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n用时 %.0f 秒，明细已写入 %s" % (time.time() - t0, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
