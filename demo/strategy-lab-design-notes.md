# Strategy Lab Demo Design Notes

## Design Direction

The Strategy Lab UI should feel like a modern quant research dashboard, not a generic admin panel or a marketing page.

The target tone is:

- Calm, precise, data-first.
- Modern glass card surfaces, but not decorative or loud.
- Light neutral background with subtle cool color atmosphere.
- Blue as the primary action/navigation color.
- Green/red/amber used only as data semantics.
- Tables remain functional and scannable.

The interface should look like a tool used repeatedly for strategy comparison, not a landing page.

## Current Demo Principles

### 1. Glass Without Fake Borders

Cards use:

- Semi-transparent white background.
- `backdrop-filter: blur(...) saturate(...)`.
- Soft outer shadow.
- Top-only inner highlight.
- Large radius around `18px`.

Avoid:

- Bottom inset borders.
- Strong gradient border lines.
- Heavy drop shadows.
- Colored glowing outlines around cards.

The final preferred card feel is subtle glass, not "gradient-bordered card".

### 2. No Background Grid

The blue grid background was removed.

Reason:

- It made the UI feel less refined.
- It visually competed with card shadows.
- Earlier warm/grid treatments created a "paper ledger" feeling.

Use only:

- A light neutral background.
- Very soft radial cool light spots.
- No repeated grid/graph-paper texture.

### 3. Card Hierarchy Comes From Space, Not Lines

Internal hard dividers made the glass cards feel cheap.

For form sections:

- Do not use full-width `border-bottom` lines.
- Use spacing, light section blocks, and small section labels.
- Keep related controls grouped in soft, low-contrast regions.

For card headers:

- Do not use a hard line between header and body.
- Use padding and typography hierarchy instead.

For data tables:

- Lines are acceptable, but they must be very faint.
- Tables need scanability, so do not remove all structure there.

## Specific Pitfalls We Found

### Pitfall 1: Warm Paper Palette Felt Old

The first demo used warm off-white, beige, brown, and amber tones.

Problem:

- It felt like a retro ledger or old financial form.
- It did not match the quant dashboard expectation.

Fix:

- Move to cold whites, light gray, blue accents.
- Keep semantic colors clear and modern.

### Pitfall 2: White/Gray Header Gradient Looked Cheap

The card header initially used a visible white-gray gradient.

Problem:

- It looked like a default admin template.
- It made the top of each card feel heavy.

Fix:

- Card headers are transparent or plain.
- Separate header/body with spacing, not a hard gradient strip.

### Pitfall 3: Rounded Card With Straight Header Corners

The card had rounded outer corners, but the header looked like a straight rectangle inside it.

Problem:

- The card corner looked clipped.
- It suggested mismatched layers.

Fix:

- Either let the card clip children with `overflow: hidden`, or do not give header an independent background.
- If a header background is needed, it must inherit top radii.

### Pitfall 4: Gradient Progress Bars Looked Unrefined

The portfolio weight bars used blue-cyan gradients.

Problem:

- The gradient did not add meaning.
- It looked decorative and slightly cheap.

Fix:

- Use a thin single-color blue bar.
- Add only a very subtle glow if needed.

### Pitfall 5: Green Toggle Was Too Loud

Some secondary toggles used a saturated green active state.

Problem:

- Green should mean positive performance/profit in this UI.
- The toggle distracted from actual data semantics.

Fix:

- Use blue/gray for interactive state.
- Reserve green for positive metrics.

### Pitfall 6: Edge Highlight Looked Like Gradient Border

The glass card had a 1px linear highlight across the edge.

Problem:

- It looked like a fake gradient border, not glass.
- It drew attention to the card boundary instead of the content.

Fix:

- Remove edge-running highlights.
- Use internal soft reflection:
  - subtle radial highlight near the upper card surface;
  - low opacity;
  - clipped inside card radius.

### Pitfall 7: "Double Chin" At Card Bottom

The card bottom showed an extra visual layer.

Cause:

- Outer border.
- Bottom `inset 0 -1px` shadow.
- Outer drop shadow.

These stacked into two bottom edges.

Fix:

- Remove bottom inset shadow.
- Keep only top inner highlight.
- Keep outer shadow light.

### Pitfall 8: Shadow Was Too Heavy

The card shadows became too large and overlapped visually.

Problem:

- Cards looked muddy near each other.
- Shadow was no longer readable as elevation.

Fix:

- Reduce main shadow to approximately:

```css
--shadow: 0 8px 22px rgba(15, 23, 42, 0.052);
```

- Keep small cards even lighter.
- Use border, transparency, and spacing for separation.

## Recommended CSS Patterns

### Main Card

```css
.panel {
  position: relative;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.72);
  border-radius: 18px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.82), rgba(255, 255, 255, 0.58)),
    rgba(255, 255, 255, 0.62);
  box-shadow:
    0 8px 22px rgba(15, 23, 42, 0.052),
    inset 0 1px 0 rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(22px) saturate(1.18);
}
```

Important:

- Do not add `inset 0 -1px`.
- Do not add a 1px gradient line at the card edge.

### Soft Internal Reflection

```css
.panel::before {
  content: "";
  position: absolute;
  inset: 1px;
  border-radius: 17px;
  background:
    radial-gradient(circle at 22% 0%, rgba(255, 255, 255, 0.56), transparent 30%),
    linear-gradient(135deg, rgba(255, 255, 255, 0.22), transparent 42%);
  opacity: 0.72;
  pointer-events: none;
}
```

This should read as surface reflection, not border.

### Form Section Group

```css
.fieldset {
  display: grid;
  grid-template-columns: 132px minmax(0, 1fr);
  gap: 12px;
  padding: 14px;
  border: 0;
  border-radius: 12px;
  background: rgba(248, 250, 252, 0.48);
}
```

Avoid full-width separator lines inside form cards.

### Data Lines

```css
th,
td {
  border-bottom: 1px solid rgba(217, 224, 234, 0.48);
}
```

Data tables can keep faint structure. Non-table cards should avoid hard lines.

## Migration Checklist For `/strategy-lab`

When applying the demo style to the real page:

- Preserve all existing IDs used by JS.
- Preserve `data-tab-panel` and `data-tab` behavior.
- Preserve `onclick` handlers for run, score, details, sorting, and range presets.
- Update Plotly colors to match the new palette.
- Replace old blue/coral palette with the new variables.
- Remove hard dividers from setup/reference cards.
- Keep table lines faint.
- Avoid bottom inset shadows.
- Avoid background grid.
- Verify `/strategy-lab` loads with HTTP 200.
- Verify Plotly JS still loads.
- Verify run button still calls `/api/strategy-lab/run`.
- Verify score button still calls `/api/strategy-lab/score`.
