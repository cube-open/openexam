"""可选依赖探测。

openpyxl / reportlab 只在「导出 Excel」「生成 PDF」时需要，不是跑起来的前提。
本模块统一判断它们是否存在，让视图在缺失时给出可执行的提示，而不是 500 堆栈。

用法：
    from .. import deps
    if not deps.has("openpyxl"):
        flash(deps.message("openpyxl"), "error")
        return redirect(...)
"""
import importlib.util

# 缺依赖时给用户看的安装命令
INSTALL_HINT = {
    "openpyxl": "pip install openpyxl",
    "reportlab": "pip install reportlab",
}

# find_spec 要遍历 sys.path，缓存一下（进程内依赖不会中途出现或消失）
_CACHE = {}


def has(name):
    """模块是否可用。只做探测，不真正 import，避免副作用。"""
    cached = _CACHE.get(name)
    if cached is None:
        try:
            cached = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            cached = False
        _CACHE[name] = cached
    return cached


def hint(name):
    return INSTALL_HINT.get(name, "pip install %s" % name)


def message(name):
    """给 flash 用的一句话。"""
    return "未安装 %s，无法完成该操作。请执行：%s" % (name, hint(name))


def missing(names=("openpyxl", "reportlab")):
    return [n for n in names if not has(n)]
