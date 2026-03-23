# TrainingEdge — UI/UX Design System

## Design Principles

1. **Apple HIG 暗色主题** — 参考 iOS/macOS 深色模式，不是纯黑，而是层次化深灰
2. **Glassmorphism** — 毛玻璃卡片 + 半透明边框，营造深度感
3. **信息密度优先** — 运动员需要一眼看到关键数据，不要过度留白
4. **中文优先** — 所有 UI 文案使用中文，数字使用等宽字体
5. **"结论→证据→动作" 页面结构** (v0.5.0+) — 每个页面按三层组织: 顶部放可执行结论，中部放支撑数据/图表，底部放操作按钮。删除低解释性、伪精确、装饰型模块

## Color Tokens

```css
:root {
    --bg-primary: #000000;
    --bg-elevated: rgba(28,28,30,0.6);
    --bg-secondary: rgba(44,44,46,0.5);

    --text-primary: #f5f5f7;
    --text-secondary: rgba(245,245,247,0.6);
    --text-tertiary: rgba(245,245,247,0.35);

    --blue: #0a84ff;       /* 骑行、中性指标、主操作 */
    --green: #30d158;      /* 跑步、正向指标、完成状态 */
    --red: #ff453a;        /* 偏离、错误、负值、高危、关键课标签 */
    --orange: #ff9f0a;     /* 警惕、TSS、注意事项 */
    --yellow: #ffd60a;     /* 中等警告、轻度偏差 */
    --purple: #bf5af2;     /* AI 生成内容、力量训练、辅助课标签 */

    --glass-border: rgba(255,255,255,0.06);
    --glass-border-hover: rgba(255,255,255,0.12);
    --shadow: 0 8px 32px rgba(0,0,0,0.4);
}
```

## Typography

- **字体栈**: `-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif`
- **数字**: font-variant-numeric: tabular-nums (等宽数字)

### 字体层级 (v0.5.0+)

| 层级 | 用途 | 大小 | 粗细 | 示例 |
|------|------|------|------|------|
| L1 | 页面结论 | 28px | 700, letter-spacing: -0.8px | 今日建议: 可执行关键课 |
| L2 | 关键数值 | 20-24px | 700 | CTL 62.5、TSB -15.8 |
| L3 | 卡片标题 | 17px | 600 | 恢复证据、约束满足情况 |
| L4 | 描述文字 / 正文 | 14px | 400 | 详细说明、证据描述 |
| L5 | 辅助文字 | 13px | 400, color: var(--text-secondary) | 数据来源、时间戳 |
| L6 | 标签/提示 | 11-12px | 500-600 | 角色标签、状态 pill |

## Spacing Scale (8px grid)

- 4px — 紧凑间距 (tag 内部)
- 8px — 元素间最小间距
- 12px — 卡片内部 padding
- 16px — 段落/区块间距
- 20px — 大区块间距
- 24-28px — 卡片 padding

## Border Radius

- 4px — 小标签 (workout-role)
- 8px — tag、按钮内标签
- 10px — 输入框、小卡片
- 12px — 中卡片
- 14px — 日历格子、摘要栏
- 16px — AI 面板
- 20px — 模态框

## Component Patterns

### Card (`.card`)
```css
background: var(--bg-elevated);
backdrop-filter: blur(40px);
-webkit-backdrop-filter: blur(40px);
border: 0.5px solid var(--glass-border);
border-radius: 14px;
padding: 16px 20px;
```

### Button — Primary (`.btn-ai-primary`)
```css
background: linear-gradient(135deg, var(--purple), var(--blue));
color: #fff;
padding: 10px 24px;
border-radius: 12px;
font-weight: 600;
```

### Button — Secondary
```css
background: rgba(255,255,255,0.08);
color: var(--text-secondary);
/* hover: background: rgba(255,255,255,0.14) */
```

### Input / Select / Textarea
```css
background: rgba(255,255,255,0.06);
border: 0.5px solid var(--glass-border);
border-radius: 10px;
padding: 10px 12px;
color: var(--text-primary);
/* focus: border-color: var(--blue) */
```

### Modal
```css
/* overlay */
background: rgba(0,0,0,0.6);
backdrop-filter: blur(8px);

/* modal body */
background: var(--bg-elevated);
backdrop-filter: blur(60px);
border-radius: 20px;
padding: 28px;
```

### Progress Bar
```css
/* background */
background: rgba(255,255,255,0.06);
border-radius: 6px;
height: 10px;

/* fill: use var(--green/--orange/--red) based on percentage */
```

## Sport Color Mapping

| Sport | Background | Border |
|-------|-----------|--------|
| cycling | rgba(10,132,255,0.1) | rgba(10,132,255,0.2) |
| running | rgba(48,209,88,0.1) | rgba(48,209,88,0.2) |
| strength/training | rgba(191,90,242,0.1) | rgba(191,90,242,0.2) |
| rest/stretch | rgba(245,245,247,0.04) | rgba(245,245,247,0.08) |

## Workout Role Tags (v0.6.0 更新)

| Role | Class | Background | Color | 标签文字 |
|------|-------|-----------|-------|---------|
| 关键课 | `.key` | rgba(255,69,58,0.15) | var(--red) | 🔴 关键课 |
| 恢复 | `.recovery` | rgba(48,209,88,0.12) | var(--green) | 🟢 恢复 |
| 辅助 | `.support` | rgba(191,90,242,0.12) | var(--purple) | 🟣 辅助 |
| 正常 | `.normal` | rgba(10,132,255,0.12) | var(--blue) | 🔵 正常 |
| 耐力 | `.endurance` | rgba(10,132,255,0.12) | var(--blue) | 耐力 |

## Page States

每个页面必须处理以下状态:

1. **空状态** — 无数据时显示引导提示 (如 "请先同步活动")
2. **加载状态** — 长操作显示 loading 动画或提示文字
3. **错误状态** — API 失败时显示错误信息，不能白屏
4. **正常状态** — 有数据的完整展示

## Responsive Breakpoints

- **Desktop**: > 900px — 7列日历网格，2列统计栏
- **Mobile**: ≤ 900px — 单列布局，堆叠卡片

```css
@media (max-width: 900px) {
    .cal-grid { grid-template-columns: 1fr; }
    .stats-row { grid-template-columns: 1fr; }
}
```

## Collapsible Sections Pattern (v0.5.0+)

非核心证据和详情默认折叠，使用 HTML `<details>/<summary>`:

```html
<details class="evidence-section">
    <summary>AI 决策依据</summary>
    <div class="evidence-content">
        <!-- 折叠内容 -->
    </div>
</details>
```

```css
details.evidence-section summary {
    cursor: pointer;
    font-weight: 600;
    color: var(--text-secondary);
    padding: 8px 0;
}
details.evidence-section[open] summary {
    color: var(--text-primary);
}
```

适用场景: AI 决策依据、完整分析展开、趋势结论详情、数据来源表

## Footer Disclaimer Pattern (v0.5.0+)

所有三个主页面 (面板/计划/身体数据) 底部显示免责声明:

```css
.page-disclaimer {
    margin-top: 32px;
    padding: 16px 20px;
    background: rgba(255,255,255,0.03);
    border-radius: 10px;
    color: var(--text-tertiary);
    font-size: 12px;
    line-height: 1.6;
    text-align: center;
}
```

声明内容: 明确"不替代医疗诊断"，仅供训练参考

## Color Semantic Rules (v0.5.0+)

统一颜色语义，所有页面一致:

| 语义 | 颜色 | 使用场景 |
|------|------|---------|
| 骑行 / 中性 | var(--blue) | 骑行运动类型、中性指标、正常状态标签 |
| 正向 / 跑步 | var(--green) | 跑步类型、完成状态、恢复标签、正向趋势 |
| 警惕 | var(--yellow) / var(--orange) | TSS 相关、轻度偏差、注意事项 |
| 偏离 / 关键 | var(--red) | 明显偏差、关键课标签、异常警告 |
| AI / 力量 | var(--purple) | AI 生成内容、力量训练、辅助课标签 |

## Emoji Usage

运动类型图标统一使用 emoji:
- 骑行: 🚴 `&#x1F6B4;`
- 跑步: 🏃 `&#x1F3C3;`
- 力量: 💪 `&#x1F4AA;`
- 休息: 🧘 `&#x1F9D8;`
- AI: ✨ `&#x2728;`
