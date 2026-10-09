"""Symbol extraction accuracy test.

Tests extract_a_share_symbols and needs_disclaimer against labeled cases.
Computes precision, recall, F1 for symbol extraction and accuracy for
disclaimer classification.

Run inside the app container:
    docker exec <app> /app/.venv/bin/python /app/scripts/test_symbol_accuracy.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.symbol_extract import extract_a_share_symbols, needs_disclaimer

# ---- Test cases: (input_text, expected_symbols, expected_disclaimer) ----
# Covers: bare codes, prefixed codes, multiple codes, Chinese-adjacent codes,
#         non-matches (inside longer identifiers), financial keywords only,
#         and casual chit-chat.
CASES = [
    # Bare 6-digit A-share codes
    ("帮我查一下600519的行情", ["sh600519"], True),
    ("600519是什么股票", ["sh600519"], True),
    ("600519 000858", ["sh600519", "sz000858"], True),
    # Prefixed codes
    ("查询 sh600519 的股价", ["sh600519"], True),
    ("sh600519的行情怎么样", ["sh600519"], True),
    ("查询sz000001平安银行", ["sz000001"], True),
    # Multiple codes
    ("分析一下sz000858和sh600519", ["sz000858", "sh600519"], True),
    # Chinese-adjacent (the \b boundary bug case)
    ("帮我查一下600519的行情", ["sh600519"], True),
    ("茅台600519跌了", ["sh600519"], True),
    # Should NOT match (inside longer identifiers)
    ("x600519", [], True),  # prefixed by ASCII letter
    ("6005190", [], True),  # 7-digit number, not a valid code
    ("abc123def", [], False),  # pragma: allowlist secret - no stock code
    # Financial keywords only (no code) — disclaimer True, symbols empty
    ("帮我看看茅台的股价", [], True),
    ("持仓分析", [], True),
    ("最近股市怎么样", [], True),
    # Casual chit-chat — no disclaimer, no symbols
    ("你好呀", [], False),
    ("今天天气真好", [], False),
    ("谢谢", [], False),
]


def test_symbol_extraction():
    """Test extract_a_share_symbols precision/recall/F1."""
    tp = 0  # correctly extracted
    fp = 0  # extracted but shouldn't be
    fn = 0  # should be extracted but wasn't
    correct_cases = 0

    print("=" * 70)
    print("  Symbol Extraction Accuracy Test")
    print("=" * 70)
    print(f"  Test cases: {len(CASES)}\n")

    for i, (text, expected, _) in enumerate(CASES):
        actual = extract_a_share_symbols(text)
        expected_set = set(expected)
        actual_set = set(actual)

        tp += len(expected_set & actual_set)
        fp += len(actual_set - expected_set)
        fn += len(expected_set - actual_set)

        match = "PASS" if actual_set == expected_set else "FAIL"
        if match == "PASS":
            correct_cases += 1

        print(f"  [{match}] Case {i + 1:2d}: {text[:40]:40s}")
        if match == "FAIL":
            print(f"         expected: {expected}")
            print(f"         actual:   {actual}")

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    case_accuracy = correct_cases / len(CASES)

    print(f"\n  {'=' * 50}")
    print("  Results:")
    print(f"  {'=' * 50}")
    print(f"  Case Accuracy:  {correct_cases}/{len(CASES)} = {case_accuracy * 100:.1f}%")
    print(f"  Precision:      {precision * 100:.1f}%  (TP={tp}, FP={fp})")
    print(f"  Recall:         {recall * 100:.1f}%  (TP={tp}, FN={fn})")
    print(f"  F1 Score:       {f1 * 100:.1f}%")
    print(f"  {'=' * 50}")

    return case_accuracy, precision, recall, f1


def test_disclaimer():
    """Test needs_disclaimer accuracy."""
    correct = 0
    tp = 0  # correctly identified as needing disclaimer
    tn = 0  # correctly identified as not needing
    fp = 0  # false positive (said needs but shouldn't)
    fn = 0  # false negative (said doesn't need but should)

    print(f"\n{'=' * 70}")
    print("  Disclaimer Classification Accuracy Test")
    print(f"{'=' * 70}")
    print(f"  Test cases: {len(CASES)}\n")

    for i, (text, _, expected) in enumerate(CASES):
        actual = needs_disclaimer(text)
        match = "PASS" if actual == expected else "FAIL"
        if match == "PASS":
            correct += 1
            if actual:
                tp += 1
            else:
                tn += 1
        else:
            if actual:
                fp += 1
            else:
                fn += 1

        print(f"  [{match}] Case {i + 1:2d}: {text[:40]:40s} => {actual}")

    accuracy = correct / len(CASES)

    print(f"\n  {'=' * 50}")
    print("  Results:")
    print(f"  {'=' * 50}")
    print(f"  Accuracy:  {correct}/{len(CASES)} = {accuracy * 100:.1f}%")
    print(f"  TP={tp}  TN={tn}  FP={fp}  FN={fn}")
    print(f"  {'=' * 50}")

    return accuracy


def main():
    """Report symbol/disclaimer accuracy and exit non-zero on regression."""
    sym_acc, sym_prec, sym_rec, sym_f1 = test_symbol_extraction()
    dis_acc = test_disclaimer()

    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(
        f"  Symbol Extraction:  accuracy={sym_acc * 100:.1f}%  "
        f"precision={sym_prec * 100:.1f}%  recall={sym_rec * 100:.1f}%  F1={sym_f1 * 100:.1f}%"
    )
    print(f"  Disclaimer Class:   accuracy={dis_acc * 100:.1f}%")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
