"""
只安装「中文 ⇄ 俄文」所需的四个离线模型包，约 474 MB。

为什么要单独写这个脚本：
LibreTranslate 默认会把索引里全部 100 个模型都下载下来（好几个 GB），
而我们只需要 zh<->en 和 en<->ru 这四个。因为官方模型库里没有中俄直连模型，
翻译实际走的是「中文 -> 英文 -> 俄文」两段跳转。

用法（两种都行）：
    1) 双击仓库根目录下的「安装中俄模型.bat」
    2) 命令行：python scripts/install_zh_ru.py

跑完之后，用这条命令验证：
    python scripts/install_zh_ru.py --check
"""

import sys
import time

# 这四个包是中俄互译的最小集合
WANTED_PAIRS = [
    ("zh", "en"),   # 中文 -> 英文
    ("en", "ru"),   # 英文 -> 俄文
    ("ru", "en"),   # 俄文 -> 英文
    ("en", "zh"),   # 英文 -> 中文
]


def already_installed():
    """返回已装好的语言对集合。"""
    from argostranslate import package

    result = set()
    for pkg in package.get_installed_packages():
        result.add((pkg.from_code, pkg.to_code))
    return result


def install_all():
    from argostranslate import package

    print("正在联网获取模型索引 ...")
    package.update_package_index()
    available = package.get_available_packages()
    print("索引里共有 %d 个模型包，我们只装需要的 4 个。" % len(available))

    have = already_installed()
    done = 0
    for src, tgt in WANTED_PAIRS:
        if (src, tgt) in have:
            print("[跳过] %s -> %s 已经装好了" % (src, tgt))
            done += 1
            continue
        t0 = time.time()
        print("[开始] %s -> %s 下载中，请耐心等待 ..." % (src, tgt))
        ok = package.install_package_for_language_pair(src, tgt)
        cost = time.time() - t0
        if ok:
            print("[完成] %s -> %s 用时 %.0f 秒" % (src, tgt, cost))
            done += 1
        else:
            print("[失败] %s -> %s 没装上。请检查网络后重新运行本脚本，"
                  "它会自动跳过已装好的部分。" % (src, tgt))
    return done


def check():
    """验证安装结果，并顺带试一句真实翻译，看英文跳转是否生效。"""
    from argostranslate import translate

    have = already_installed()
    print("=== 已安装的模型 ===")
    for s, t in sorted(have):
        print("  %s -> %s" % (s, t))
    missing = [p for p in WANTED_PAIRS if p not in have]
    if missing:
        print("还缺：", missing)
        return False

    print("\n=== 实测：中文能否翻到俄文 ===")
    langs = {l.code: l for l in translate.get_installed_languages()}
    src, tgt = langs.get("zh"), langs.get("ru")
    if src is None or tgt is None:
        print("语言没加载出来，请重启程序再试。")
        return False

    translation = src.get_translation(tgt)
    if translation is None:
        print("结果：系统找不到 zh -> ru 的路径，需要自己实现两段式跳转。")
        return False

    for text in ["数据结构", "辅导员", "本学期共修读 5 门必修课"]:
        print("  中文：%s" % text)
        print("  俄文：%s" % translation.translate(text))
    print("\n注意：经过英文中转，术语很可能被翻坏 —— 这正是术语库要解决的问题。")
    return True


def main():
    if "--check" in sys.argv:
        ok = check()
        sys.exit(0 if ok else 1)
    n = install_all()
    print("\n装好了 %d / 4 个。" % n)
    if n == 4:
        print("下一步：运行  python scripts/install_zh_ru.py --check  验证效果。")


if __name__ == "__main__":
    main()
