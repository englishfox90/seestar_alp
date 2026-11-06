# SSC design

The design language and framework for the whole web UI. The owner approved this on 2026-11-05. Check off work here as it lands.

- **Earlier mockup, kept for reference only:** [Seestar Dashboard Redesign](https://claude.ai/artifact/KKZCNBzfnAg2hJqBkYnXKw). The specification below overrides it wherever they differ.
- **Tokens:** `front/public/ssc/tokens.css`. **Legacy skin:** `front/public/design.css`.
- **Enforced by:** `tests/test_design_rules.py`.

## Owner decisions that amend the specification

- **Pier on phones:** a larger thumbnail with tap to swap instead of the 64px porthole. At least 120px wide, with the final size set on a real phone during the prototype. Desktop shows both images in full.
- **Start and Stop:** Stop goes on Tonight, with a confirm sheet. Start stays off Tonight until there is a start that respects the roof. Check that pressing Stop twice is harmless before building it.
- **Live wording:** the Roof page says "Live" rather than "Armed" or "Automation on", because that banner means dry run is off, not that automation is enabled.

- **Red floor corrected:** the specification's `filter` on `html` also greys the red overlay, which lets green and blue through. `red-floor.css` is the version in force: the multiply overlay alone, with images and canvases dimmed directly. The specification's Red contrast figures assume no extra dimming on text, which is now true.

- **One scope:** the owner runs a single scope, so multi-scope cases only need to stay working, not be refined.
- **Repeat Stop presses:** pressing Stop again reruns the sequence and can reset the park-attempt count. Tonight and Automation disable the button after a press. The owner chose to leave the server route unguarded for now.
- **Bundled font:** none. Icons are Phosphor (`ssc/icons.svg`) and polled regions use idiomorph (`ssc/vendor/`).

## Status

### Done: foundation

- [x] Tokens for Day, Night and Red, including the larger type in Red
- [x] Red floor, and a device-only Red night mode in the More menu. **Not yet tested on an iPhone.**
- [x] Legacy skin moved onto the tokens: type scale, 600 as the only strong weight, new colours
- [x] One shared page edge for nav, titles and panels, including pages with a nested `.container`
- [x] Dashboard columns end on the same line
- [x] Docked tab bar. Scope switcher and roof status always visible on phones. More opens straight onto its list.
- [x] Status badges are quiet, with only danger solid. Selected rows are no longer an accent block.
- [x] Fields are not marked invalid until touched. Roof's normal banner is no longer red.
- [x] No looping animation on the dashboard
- [x] Pier camera on the dashboard from PFR Sentinel, with stale and offline warnings
- [x] Drift tests: raw values banned under `ssc/`, legacy inline styles can only fall

### Done: rebuild

- [x] Tonight on the new shell: all states, Night Line with roof history, morning card, image viewer, Stop with confirm
- [x] Tonight, Plan, Scope and System with a shared sub-navigation, on old and new pages
- [x] Targets, Automation and Settings on the new shell
- [x] Schedule, System, Live, Prepare and capture pages restyled on `base.html`
- [x] Per-device Auto, Day, Night and Red
- [x] Phosphor icons and idiomorph

### Next

- [ ] Check on a real iPhone, including Red, the Live video and joystick
- [ ] A start that respects the roof, as its own change, with a real test on the scope
- [ ] Per-target images in the morning card, and common target names, which need a catalog lookup
- [ ] Move Schedule and Live onto the shell, if they earn it

### Known gaps

- Bootstrap's small checkbox radius and some stray sizes remain on legacy pages until each is moved.
- The `webui_*` risks in the first review have not been tested, because testing writes config.

## Decision log

- **2026-11-06:** Final design review: the language is done. Phone Tonight now puts the Night Line directly under the sentence, because a glance has to show the night. Image corners are 0 everywhere. Schedule and Live stay on Bootstrap unless they earn the move.
- **2026-11-05:** Keep htmx and server templates. New screens get their own shell without Bootstrap, and legacy pages stay on it.
- **2026-11-05:** The first mockup, with glass panels and teal and coral accents, was rejected as AI-looking. The second, with system colours, was approved and then overturned in part by the design review.
- **2026-11-05:** Images are never cropped by the UI, except the pier thumbnail's sky circle.

---

## Design specification

**What the images dictate**
- **Stack:** 720×1280, stars edge to edge, mean luminance 40/255. Thin text over it fails.
- **Pier:** 2048×2048 fisheye, mean 52, peak 255. Burned-in text fills all four corners, including a bright red timestamp. At 344px that text is about 4px tall, and at 550px about 7px. It is decoration except in the viewer.

### 1. Principles
1. **The image is the only colour.** UI stays neutral. Colour appears only for interaction and status.
2. **Nothing sits on an image.** Captions and controls go beside or below it. Images are never cropped.
3. **Every state has one sentence and at most one action.** No fact is said twice.
4. **Status works without colour.** Icon, word and position must carry it, because Red mode has no hues.
5. **Light is a cost.** Each lit pixel must earn its place after dark, so Night avoids large bright fills.
6. **Same edge, same size, or a reason.** Alignment comes from templates, and a test enforces it.

**"Apple-inspired" means** hierarchy through type weight and space, grouped lists, inset hairlines, sheets, and restrained materials. **It must not mean** iOS stock blue on everything, floating pills, blur everywhere, large titles for their own sake, or any Apple asset.

**"Astrophotography" means** instrument-like calm, real data, dark-adaptation care and tabular precision. **It must not mean** starfield backgrounds, nebula gradients, glows, orbit graphics or space fonts.

### 2. Type
**Decision: system stack, no bundled font.**
`system-ui, -apple-system, "Segoe UI Variable Text", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`
It needs no network, gives San Francisco on the owner's phone, and is the lightest option. Remove `Inter` from the stack and the "Bundle Inter" task.

| Role | Phone | ≥768px | Weight | Tracking |
|---|---|---|---|---|
| Page title | 24/30 | 28/34 | 600 | −0.01em |
| Status sentence | 20/26 | 22/28 | 600 | −0.005em |
| Metric value | 28/32 | 28/32 | 600 | tabular |
| Section title | 17/22 | 17/22 | 600 | 0 |
| Body, row | 15/20 | 15/20 | 400, 600 | 0 |
| Input text | 16/22 | 15/20 | 400 | 0 |
| Footnote | 13/18 | 13/18 | 400 | 0 |
| Caption, tab label | 12/16 | 12/16 | 400, 600 | 0 |

- **Overturned:** 34px titles (the owner said things are oversized), weight 700, and 11px captions (too small at night).
- **Inputs are 16px on phones** because iOS Safari zooms focused fields smaller than that.
- **Numbers:** `font-variant-numeric: tabular-nums` on values, times and metrics. Units are 15px at 400 weight.
- **Red mode:** body 16/22, footnote 15/20, caption 13/18, and no 400 weight under 16px.

### 3. Colour

| Token | Day | Night | Red |
|---|---|---|---|
| `--ssc-canvas` | #f5f5f7 | #000000 | #000000 |
| `--ssc-surface` | #ffffff | #131315 | #140000 |
| `--ssc-raised` | #ffffff | #1c1c1f | #200000 |
| `--ssc-text-1` | #1d1d1f | #e8e8ea | #d40000 |
| `--ssc-text-2` | #55555c | #a3a3a9 | #a80000 |
| `--ssc-text-3` | #6b6b73 | #85858c | not used |
| `--ssc-separator` | rgba(60,60,67,.18) | rgba(255,255,255,.10) | #3a0000 |
| `--ssc-fill-1` | rgba(118,118,128,.12) | rgba(255,255,255,.07) | #1f0000 |
| `--ssc-fill-2` | rgba(118,118,128,.20) | rgba(255,255,255,.12) | #2e0000 |
| `--ssc-accent` | #0a62c9 | #5fa4f8 | #d40000 |
| `--ssc-accent-tint` | #e3edf8 | #1c2a3c | #2e0000 |
| `--ssc-on-accent` | #ffffff | #000000 | #000000 |
| `--ssc-green` | #1a7f37 | #3ec46d | = text-1 |
| `--ssc-orange` | #a85400 | #f0a030 | = text-1 |
| `--ssc-red` | #c1271d | #ff6259 | = text-1 |
| `--ssc-danger-fill` | #c1271d | #a8201a | #c80000 |
| `--ssc-on-danger` | #ffffff | #ffffff | #000000 |

**Contrast (WCAG, computed), on canvas / surface**

| Pair | Day | Night | Red |
|---|---|---|---|
| text-1 | 15.46 / 16.83 | 17.16 / 15.17 | 3.80 / 3.69 |
| text-2 | 6.79 / 7.39 | 8.37 / 7.40 | 2.66 / 2.59 |
| text-3 | 4.85 / 5.28 | 5.73 / 5.06 (4.64 on raised) | not used |
| accent | 5.35 / 5.83 | 8.15 / 7.20 | 3.80 / 3.69 |
| green | 4.66 / 5.08 | 9.32 / 8.24 | not applicable |
| orange | 4.90 / 5.34 | 9.78 / 8.64 | not applicable |
| red | 5.39 / 5.87 | 7.14 / 6.31 | not applicable |
| on-accent on accent | 5.83 | 8.15 | not used |
| on-danger on danger fill | 5.87 | 7.28 | 3.46 |

Red cannot meet AA: pure `#ff0000` on black tops out at 5.25:1. That is why Red has larger type and only two text levels. Red is an exception for dark-adapted use only, so it is never the default.

**Decisions on what was approved**
- **True black: kept for Night and Red.** `contain` letterboxing becomes invisible, which no dark grey offers.
- **`#1c1c1e` surfaces: overturned.** `#131315` emits less light and competes less with the image.
- **System blue: replaced by a calmer blue pair.** Stock `#0a84ff` fails with white text (3.65:1) and reads as default iOS.
- **Indigo: removed.**

**How Red protects dark adaptation.** Rods are least sensitive to long wavelengths, so green and blue emission must be zero and luminance low.
- Tokens alone do not guarantee that, because legacy pages, canvases and images leak.
- Use a floor on every page:
```css
/* Multiplying by pure red leaves only the red channel. A filter on html would grey this overlay too. */
html[data-theme="red"] body::after{content:"";position:fixed;inset:0;z-index:2147483647;
  background:#f00;mix-blend-mode:multiply;pointer-events:none}
html[data-theme="red"] img,html[data-theme="red"] canvas,html[data-theme="red"] video{filter:brightness(.7)}
```
- This zeroes green and blue beneath it, including on legacy pages.
- **Limits:**
  - The web cannot lower the backlight, or control the browser's or OS's own UI.
  - Native `confirm()` and select menus can flash light, so Red needs custom sheets. `confirm()` is currently in `roof.html` and `target_list.html`.
  - Top-layer `<dialog>` may escape the overlay, so sheets use Red tokens anyway. Test this.
  - For a real pier session, the user should also enable the phone's red colour filter.
  - Set `color-scheme` and `theme-color` to black.

**webui_* settings.** All are empty in `device/config.toml` today.

| Setting | Treatment |
|---|---|
| `accent_color` | Keep in Day and Night, ignored in Red. A small server helper picks the on-accent colour. |
| `font_family` | Keep, as `--ssc-font`. |
| `font_url` | Legacy only, since it conflicts with offline use. |
| `text_color`, `link_color` | Legacy pages only, ignored in the new shell. They flatten hierarchy and break Night and Red. Label them in Settings and in release notes rather than removing them silently. |

### 4. Depth and materials
- **Levels:** canvas, surface, raised (sheets and menus). No shadows, except a sheet scrim of `rgba(0,0,0,.5)`.
- **Glass:** only the top bar and tab bar, at `saturate(1.4) blur(20px)` with 88% opacity. Over stars, low opacity reads as noise. Red is fully opaque.
- **Tab bar: overturned, now docked.** The 28px floating pill wastes 32px of width, sits over images, and is the most trend-bound element in the mockup. Use a full-width bar of 50px plus safe area, with a hairline top.
- **Radii:** 10 panels, 8 controls, 14 sheet top, 0 images, 50% only for dots. Kept because 12+ reads as soft and consumer, and 6 reads as a developer tool, and because the owner asked for smaller corners.
- **Images:** black backing, `contain`, no border, no text on top. Caption sits below on a 13px line. Stale images use `opacity:.55` plus a stale caption. Night: `brightness(.85)`.

### 5. Layout framework

| Token | Value |
|---|---|
| Spacing | 2, 4, 8, 12, 16, 24, 32, 48 |
| Compact | <768px, 4 columns, gutter 16, gap 16 |
| Regular | 768–1199px, 8 columns, gutter 24, gap 16 |
| Wide | ≥1200px, 12 columns, gutter 24, gap 16 |
| Max width | 1280 including gutters (content 1232) |
| Form column | max 640 |
| List and detail | 360 + 1fr |

**Alignment rules**
1. Titles and panels share the page edge. Only phone images may touch x=0.
2. Panel content insets 16. Section titles sit outside panels, aligned with the panel edge.
3. Rows: label at left, value at right, baseline aligned. Separators start at the text edge and run to the panel edge.
4. Icon box is 24; text after an icon starts at 16+24+12=52.
5. Panels in one row stretch, so tops and bottoms match.
6. Controls in a row share one height: 44 on touch, 36 for fine pointers.
7. Times are 24-hour with no seconds. The live page currently mixes "09:36:01 PM" and "22:37".

**Templates**
- **T1 Hero.** Compact: top bar 44, hero slot, Now panel, then scroll. Slot height is `min(100svh − 44 − tabbar − nowPanel, (100vw − 144px) × 16/9)`, which keeps 72px of black margin beside the stack.
- **T1 wide.** `grid-template-columns: calc(var(--h)*.5625) var(--h) 1fr; gap:16px`, where `--h` = `min(100svh − 240px, 640px, (100vw − 48px − 340px − 32px)/1.5625)`. Stack and pier share exact top and bottom edges. The rail has a 340px minimum. Night Line spans the full width below.
- **T1 regular.** Same two image columns; the rail drops below.
- **T2 List and detail.** Phone: list, then pushed detail. Wide: 360 + 1fr. Tables become rows on phones.
- **T3 Form.** One 640 column of grouped rows with a sticky action bar. Wide adds optional 300px side help.
- **T4 Tool.** Full-bleed canvas or view, slim top bar, sheet or side panel of 340 for controls.

| Page (templates) | Template | Destination | Treatment |
|---|---|---|---|
| Dashboard | T1 | Tonight | Rewrite |
| Home (`index`, `home_content`) | T2 detail | Scope › Device | Rewrite, remove as a landing page |
| Roof (status) | T1 | Tonight | Rewrite |
| Roof (log) | T2 | Tonight › Activity | Rewrite |
| Roof (controls, image source, capture) | T3 | Plan › Automation | Rewrite |
| Targets | T2 | Plan | Rewrite |
| Framed Mosaic (+create) | T4 | Plan › Frame | Restyle, complex JS |
| Schedule (+13 item forms) | T2 + T3 | Plan › Sequences | Restyle, then rewrite |
| Planning | T4 | Plan | Restyle |
| Mosaic (+create) | T3 | Plan › Sequences, or Scope › Shoot now | Restyle |
| Live (+modes, `live_status`) | T4 | Scope | Restyle, then rewrite controls |
| Goto (+`goto_target`) | T3 | Scope › Shoot now | Restyle |
| Image (+create) | T3 | Scope › Shoot now | Restyle |
| Search, Position | T3 | Scope › Shoot now | Leave |
| Startup, `do_startup` | T3 | Scope › Prepare | Restyle |
| Polar align (`pa_refine`) | T4 | Scope › Prepare | Restyle |
| Settings | T3 | System | Rewrite rows only |
| Config | T3 | System | Restyle |
| Commands (+`do_command`) | T3 | System › Advanced | Restyle |
| Console, System page | T2 | System › Advanced | Leave |
| Stats | T2 | System | Restyle |
| Guest Mode | T2 | System | Restyle |
| Platform | T3 | System | Leave |
| Support | T2 | System | Leave |
| Event status, telescope list, no telescopes | T2 | System, or empty state | Leave |

I did not render Console, System, Search, Position, Config, Image, Planning, Platform or Support in this pass.

### 6. Components

| Component | Anatomy, sizes | States | Replaces |
|---|---|---|---|
| Button, primary | 15/600, height 44 (36 fine pointer), radius 8, padding 16. One per view. Night: tint fill with accent text | hover, pressed, disabled, busy | `.btn-primary` |
| Button, secondary | fill-1, text-1 | same | `.btn-secondary`, `.btn-outline-*` |
| Button, plain | accent text, 44 hit area | same | `.btn-link` |
| Stop & park | Height 48, full width on phone, red text on tinted fill. Solid only inside the confirm sheet | ready, stopping, failed inline, hidden while parking, disabled offline with reason | `.btn-danger` |
| Confirm sheet | Title, one consequence sentence, confirm 50 high, "Keep imaging" 50 high. Confirm sits away from the trigger position. Outside polled regions. No hold-to-confirm (gloves, unreliable) | open, busy, error, auto-closes if no longer valid | `confirm()`, `.modal` |
| Status line | 8px dot or 16px icon plus word, 13px | ok, attention, danger, neutral | `.badge` |
| Danger band | Fill, icon 24, 17/600 headline, 15 line, chevron, minimum 64 high. Only for active danger | active, resolved | `.alert-danger` |
| Notice | Icon, 15 text, no fill | info, warning | `.alert-*` |
| List row | Minimum 44, inset separator, optional icon, label, value, chevron | default, pressed, selected (check, never a blue block), disabled | `.list-group`, `.table`, `.stat-row` |
| Field | Label 13 above, input 44, radius 8, fill-1, help 13 below. Red only after a failed submit | default, focus 2px accent ring, error, disabled | `.form-control`, `.form-text` |
| Stepper | − value +, 44 targets, tabular, unit | min, max, disabled | number inputs |
| Toggle | 51×31 in a 44 row | on, off, busy | `.form-switch` |
| Segmented | 2–4 equal cells, height 36 in a 44 hit area, radius 8 | selected, disabled | `.nav-pills`, radio groups, short `select` |
| Picker row | Row opening a sheet list. Native `select` allowed outside Red | open, selected | `.form-select` |
| Sheet | Grabber, title, close 44, max 90svh, native `<dialog>` | open, closing | `.modal`, `.offcanvas` |
| Menu | Native `<details>`-based, raised, rows 44 | open | `.dropdown` |
| Top bar | 44: scope name menu at left, status line at right | offline, multi-scope with Federation | `.navbar` |
| Tab bar | 4 items, icon 24, label 12, regular or fill icon when active | active | `.app-tabbar` |
| Wide nav | Same 4 items, height 44, weight 600 when active | active | `.navbar-nav` |
| Image viewer | Full screen on black, pinch and pan, close 44, metadata below, pure black in Night | loading, stale, failed | new |
| Pier thumbnail | At least 120 wide on phones, whole image, tap to swap with the stack. Desktop shows both images in full, sharing top and bottom edges | stale, offline, hidden | new |
| Metric | Label 13 above, value 28, unit 15. Maximum 3, never in a tile grid | stale shows "—" | `.focus-value` |
| Bar | Height 4, radius 2, fill-2 track | value, split (kept and dropped) | `.progress`, `.metric-bar` |
| Chart | SVG, 1px grid, 2px line, tabular labels 13, no legend box, direct labels | empty, partial | focus SVG |
| Empty | One sentence, one optional action | n/a | ad hoc |
| Loading | Keep last data. First load: reserved-size blank, then "Loading…" after 1s. No spinners or overlays | n/a | `.is-loading` |
| Error, offline | Plain sentence, data age, retry only when useful | n/a | `.alert` |

Minimum touch target is 44 everywhere, including small buttons on touch.

### 7. Signature elements

**Night Line**
- Geometry: full width of its container. Track height 8 on phones, 12 at ≥768, radius 2. A `role="img"` with a full-sentence `aria-label`.
- Axis: sunset to sunrise. Times at both ends in 13px tabular.
- Layers, bottom to top:
  - Twilight: fill-1.
  - Dark window: fill-2.
  - Target blocks, 2px gaps: done text-3, now accent, next a 1px text-2 outline.
  - Roof-closed periods: 45° hatch in red, 4px pitch.
  - Now marker: 2px wide, 20 tall, text-1, static.
- Labels: target names below blocks at 13px, only if the block is ≥64px wide. Maximum 3 on phones.
- Red: fills become luminance steps, and the now marker is the brightest element.
- Rendering: server-rendered CSS with percentages through `--start` and `--end`. No JS.
- Backend:
  - Dark window and moon percent exist in the planner.
  - Target blocks exist only for the target list, so named schedules show the dark window only.
  - Sunset and sunrise need a small addition.
  - Roof history needs a **new log**.
  - Moon-up bands are optional later.

**Tonight states** (sentence in text-1 unless noted; Night Line always present)

| State | Hero | Treatment | Sentence | Action |
|---|---|---|---|---|
| Day, idle | Night Line at 24px height, plan rows | Stack hidden. Pier thumbnail | "Starts 18:12 with NGC 6992" | Edit plan (plain) |
| Waiting for dark | Pier | Sky-first, stack thumbnail | "Waiting for dark, sun −9°" | none |
| Opening, slewing | Pier, compact steps 13px below | No motion except cross-fade | "Moving to NGC 6992" | Stop |
| Autofocus | Focus curve, full slot | 2px accent line on black | "Focusing, position 1172" | Stop |
| Darks | Last image at .55, bar 4px | Bar is the only accent | "Taking darks, 40%" | Stop |
| Stacking | Stack plus pier thumbnail | Metrics: earned, kept, of goal | "Stacking NGC 6992" | Stop in the scroll area |
| Parked, held | Last image at .55 | No green | "Held. 7 h of dark left" | Link to Plan › Automation |
| Finished | Morning card, last image | Hours gained per target | "Finished. 6.2 h earned" | none |
| Roof unsafe | Pier, danger band on top | Only solid red on screen | "Roof unsafe. Parking" | none while parking |
| Now parked | Pier, band turns to notice | Band relaxes | "Parked 22:14, safe" | none |
| Offline, loop stale | Last image at .55, stale caption | Orange, danger if loop stale | "Scope offline, last seen 22:14" | none |

- **Start stays off Tonight** until it respects the roof, per the working direction. A roof-respecting start needs **new backend**. I have not confirmed `/roof/stop` is idempotent.
- **Metrics:** earned uses `session.stack.integration`, kept uses `drop_percent`, goal uses `goal_text`.
- **Desktop:** both images always visible, with the same treatments. The state's hero gets the caption emphasis.
- **Pier panel asks:** a clean image without burned-in text and a JSON of conditions would fix the red timestamp clash. This is **backend work in Sentinel, and I don't know if it already exists.**
- **Viewer:** `?full=true` is sent as an attachment for the stack, so the viewer needs an inline route.

### 8. Icons, motion, voice

**Icons.** Phosphor Icons (MIT), Regular and Fill, vendored as an SVG sprite with its licence file in `front/public/ssc/`. Regular for idle, Fill for active tabs. Sizes 16, 20, 24. I rejected Lucide because its default association with generated dashboards is the look the owner rejected. Unsure: whether Phosphor has a suitable telescope glyph. If not, draw the scope arm and roof glyphs on the same 24 grid, as `nav.html` already does.

**Motion**

| Use | Duration |
|---|---|
| Press, hover | 100ms |
| Text and state change | 160ms fade |
| Sheet in / out | 240ms / 180ms |
| New image cross-fade | 600ms |

Use `ease-out`. No loops, including `dash-pulse`, which `AGENTS.md`'s rule on unbounded animations already discourages. `prefers-reduced-motion` and Red use instant changes, except sheets at 100ms.

**Voice**
- Sentence case, no uppercase, no full stops on labels.
- Sentences open with the verb or the state, with a maximum of 2 lines on phones. Numbers come before words.
- Use `h`, `s`, `°`, and en dash-free ranges such as "22:02 to 00:07".
- Say what happens next only when it differs from what the user assumes.
- No firmware terms in Tonight (never "cancel", "ContinuousExposure").
- Rename "Armed" to "Automation on". "Stop + Park" becomes "Stop & park". "Target list" becomes "Targets". "Schedule Page" becomes "Sequences".
- Button labels name the action and object.

### 9. Technical framework

**Files**
```
front/public/ssc/ssc.css        @layer order and @imports
front/public/ssc/tokens.css  base.css  layout.css  components.css  pages.css
front/public/ssc/red-floor.css  theme.js  icons.svg
front/templates/ssc/shell.html  ui.html (macros)  nav.html
```

**Layers:** `@layer reset, tokens, base, layout, components, pages, utilities;`
- The shell does not load Bootstrap CSS or JS.
- Native `<dialog>` and `<details>` cover its needs.

**Legacy pages** keep `base.html`, Bootstrap, and the current `design.css` unlayered. They also load `tokens.css`, `red-floor.css` and the new nav CSS.
- Do not import Bootstrap into a layer. The inline `<style>` in `base.html` is unlayered and would beat every layer anyway, so layering gains nothing until those pages move.

**Bootstrap's role:** legacy pages only, never in new markup. New work in `roof/` or `ssc/` uses no `.btn`, `.card` or `.row`.

**Naming:** `.ssc-row`, `.ssc-row__value`, `.is-selected`, `[data-tone=danger]`. Tokens are `--ssc-*`.

**Macros** (`ssc/ui.html`): `button(label, kind, type, attrs)`, `stop_action()`, `row(label, value, tone, href, icon)`, `status(tone, text, icon)`, `metric()`, `field()`, `stepper()`, `toggle()`, `segmented()`, `sheet()`, `notice()`, `band()`, `bar()`, `empty()`, `night_line(data)`, `icon(name, size)`.

**Fork strategy**
- Keep new routes and templates in fork-owned paths (`roof/`, `templates/ssc/`).
- Leave upstream template filenames untouched where possible.
- `nav.html` is the accepted conflict point.
- Keep the Federation option and the `test_front_app_state.py` nav contract, updating it for the new markup.
- Unsure: how Falcon treats a second `add_route` on an existing path. Verify before relying on it for redirects.

**htmx**
- Add idiomorph (0BSD, vendored) and use `hx-swap="morph:innerHTML"` for polled regions, so focus and CSS state survive.
- Keep `hx-sync="this:drop"`, `[!document.hidden]`, `no-htmx-fx`, and the empty-response guard.
- Images are never swapped by htmx. Keep the existing preload pattern.
- Open sheets and menus sit outside polled regions.
- Reserve space for every polled value.
- A combined `/tonight/state` must read only cached data, since `/live` calls the scope today. **Backend work.** Hardware stays on 30s and loads only when Details is open.

**Theme**
- `data-theme` on `html`, values `day|night|red`, with `data-bs-theme` mapped alongside (`day`→light, others→dark).
- A blocking inline script in `<head>` reads `localStorage` before first paint.
- Default "Auto": Day when `sun_deg` > −6, otherwise Night. Red is manual only, because remote viewing should not turn red on its own.
- The server `uitheme` setting becomes the default for new devices.
- Move the switch to System, with a one-tap Red toggle in the top bar menu.

**Build step:** none. Hand-split CSS over a LAN is fine. Add `asset()` with a file-hash query to end stale-cache problems.

**Stop drift:** add `tests/test_design_rules.py`, a pytest with no Node dependency, scanning `ssc/` and `front/public/ssc/`. It fails on:
- Raw hex or rgba outside `tokens.css`.
- `font-size`, `font-weight`, `border-radius` or spacing values not from the tokens.
- `style="` attributes, except CSS-variable-only values such as `--start`.
- `<style>` blocks, `!important`, `confirm(`, and Bootstrap classes.

A ratchet test also records current inline-style counts for legacy pages, and they may only fall. It also needs template rendering checks that every route resolves from nav.

### 10. Migration plan (all estimates)

| Step | Work | Estimate |
|---|---|---|
| 1 | Write the new DESIGN.md, tokens, red floor, drift test, theme script | 2 days |
| 2 | Static prototype of Tonight states on the real phone, all three themes. Owner sign-off, including red floor on iPhone | 2 days |
| 3 | Shell, macros, nav, four destinations, redirects | 3–4 days |
| 4 | `/tonight/state`, state mapping, Tonight, Stop with confirm, Night Line | 1–1.5 weeks |
| 5 | Plan: Targets rows, Automation, roof page retired | 1 week |
| 6 | Legacy pass: fix titles, red-at-rest inputs (`base.html:707`, seen on Mosaic and Roof), selected-row blue block and Schedule wrapping | 2–3 days |
| 7 | Settings and Schedule rows, Live controls | 1–2 weeks |
| 8 | Roof history, Morning card, viewer, roof-respecting Start | 1 week |

- **Rewrite:** Dashboard, Roof, Targets, Home, nav. Settings rows later.
- **Restyle:** Schedule, Live, mosaics, Goto, Image, Startup, Stats.
- **Leave alone:** Support, Platform, Console, System, Event status.

**From `design.css`:** keep as the frozen legacy skin. Update its tokens to the new values and allow only bug fixes. Its Bootstrap overrides are not carried into `ssc/`. Delete `base.html` rules only as each page leaves.

**From `DESIGN.md`:** keep the direction, decision log, webui promise (reworded), and Phase 3 counts as the ratchet baseline. Discard the type table, 34px, the pill exception, the Inter task, Phase 2 as written, and "Pier camera beside the stack" wording, which should become the T1 rule.

### Rejected options
**Typeface**
- Bundled Inter: the common default of generated UIs, adds weight, and gains nothing on the phone.
- Bundled SF: its licence does not allow this.
- Mono or geometric faces (Space Grotesk, Plex Mono): read as developer or sci-fi, and fight the pier overlay.

**Colour**
- Keeping system colours unchanged: fails contrast and reads as stock.
- A warm amber accent: clashes with warning orange and blurs into Red.
- A neutral-only accent: hides what is tappable on an app controlling hardware.
- Filter-only Red: gives no hierarchy control, so tokens plus the floor beat either alone.

**Framework**
- SPA (React, etc.): adds a build, a rewrite and merge pain with little gain for polled fragments.
- Tailwind: needs a build and scatters design decisions into markup.
- Continuing to override Bootstrap: an endless specificity fight, and it keeps the Bootstrap look.
- Removing Bootstrap everywhere now: high cost on rarely used upstream pages, with merge conflicts.
