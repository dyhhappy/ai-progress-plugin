"""Resolve available text fonts and apply physical sizes on monitor DPI changes."""
from ..theme import tokens as T

_BASE = {name: getattr(T, name) for name in (
    'FONT_TITLE','FONT_BODY','FONT_SMALL','FONT_TINY','FONT_ICON','FONT_MONO',
    'FONT_ALERT_TITLE','FONT_HOTKEY')}


def configure_fonts(root, scale):
    from tkinter import font
    families=set(font.families(root))
    family=next((f for f in ('Microsoft YaHei UI','Microsoft YaHei','Noto Sans CJK SC',
                            'PingFang SC','Segoe UI','Arial') if f in families),
                font.nametofont('TkDefaultFont',root=root).actual('family'))
    T.FONT_FAMILY=family
    for name, spec in _BASE.items():
        face=spec[0] if name in ('FONT_MONO','FONT_HOTKEY') and spec[0] in families else family
        setattr(T,name,(face,-max(8,round(abs(spec[1])*96/72*scale)),*spec[2:]))
