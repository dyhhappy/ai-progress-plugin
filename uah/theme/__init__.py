"""uah.theme —— 视觉令牌与品牌资源层。

```
uah/theme/
├─ tokens.py      颜色 / 尺寸 / 字体 / 图标字形 / 中文文案（**唯一来源**）
├─ oc/            OC 印章（由 OC 原始设定图裁切生成；见 oc/README.md）
└─ icons/         图标规范（字形方案；见 icons/README.md）
```

宿主与组件只能从这里取样式：**不得自带色表**（``uah/tests/test_uah_phase1.py``
的 B 组断言守这条）。要把样式改一处就全站生效，改这里。
"""
