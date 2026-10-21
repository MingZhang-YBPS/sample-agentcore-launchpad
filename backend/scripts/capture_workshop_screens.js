// Page-side helpers for capture_workshop_screens.py, one per "// @NAME" section.

// @OVERLAY_JS
// One look for every highlight, matching the hand-drawn boxes of the first edition:
// a red frame with a light inner line, and a numbered red badge on its top-left corner.
({boxes, pad}) => {
  const host = document.createElement('div');
  host.id = '__ws_overlay';
  host.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none';
  document.body.appendChild(host);
  // every drawn element's page rect (badge ring included): the crop is the union of
  // these, so a badge placed outside its frame is never cut off by the crop
  const placed = [];
  const docW = document.documentElement.scrollWidth;
  const S = 28, RING = 3;
  for (const b of boxes) {
    if (!b.rect) continue;
    const r = b.rect, p = b.pad ?? pad;
    const fx = Math.max(r.x - p, 0), fy = Math.max(r.y - p, 0);
    const fw = Math.min(r.w + 2 * p, docW - fx), fh = r.h + 2 * p;
    const f = document.createElement('div');
    f.style.cssText = `position:absolute;left:${fx}px;top:${fy}px;width:${fw}px;`
      + `height:${fh}px;border:3px solid #e5243b;border-radius:9px;`
      + 'box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.75),0 0 0 1px rgba(0,0,0,.25);box-sizing:border-box';
    host.appendChild(f);
    placed.push({x: fx, y: fy, w: fw, h: fh});
    if (b.n != null) {
      const d = document.createElement('div');
      d.textContent = String(b.n);
      // default: on the frame's top-left corner; 'left' / 'right': outside the frame,
      // centred vertically; 'top': above the frame, centred — for a box whose corner
      // would cover its own label or a neighbour
      let x, y;
      if (b.badge === 'left') { x = r.x - p - 36; y = r.y + r.h / 2 - 14; }
      else if (b.badge === 'right') { x = r.x + r.w + p + 8; y = r.y + r.h / 2 - 14; }
      else if (b.badge === 'top') { x = r.x + r.w / 2 - 14; y = r.y - p - 36; }
      else { x = r.x - p - 13; y = r.y - p - 13; }
      // never past the page's own edges (a box flush with the page side)
      x = Math.min(Math.max(x, RING), docW - S - RING);
      y = Math.max(y, RING);
      d.style.cssText = `position:absolute;left:${x}px;top:${y}px;width:${S}px;height:${S}px;`
        + 'border-radius:50%;background:#e5243b;color:#fff;font:700 16px/28px system-ui,sans-serif;'
        + 'text-align:center;box-shadow:0 0 0 2px #fff,0 1px 3px rgba(0,0,0,.4)';
      host.appendChild(d);
      placed.push({x: x - RING, y: y - RING, w: S + 2 * RING, h: S + 2 * RING});
    }
  }
  return placed;
}

// @UNCLAMP_JS
// Grow the page so nothing the crop needs lives inside an inner scroller:
// unclamp the shell's fixed-height panes, then report the document height.
() => {
  const st = document.createElement('style');
  st.id = '__ws_unclamp';
  st.textContent = `html,body,#root,.v3-shell,.v3-main,.v3-page{height:auto!important;max-height:none!important;overflow:visible!important}`;
  document.head.appendChild(st);
  return Math.ceil(document.documentElement.scrollHeight);
}

// @PROBE_JS
(root) => {
  const out = [];
  const walk = (el, depth) => {
    if (depth > 9 || out.length > 900) return;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return;
    const tid = el.getAttribute('data-testid');
    const cls = (el.className && typeof el.className === 'string') ? el.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(' ').slice(0, 50);
    if (tid || cls || own) out.push(`${'  '.repeat(depth)}${el.tagName.toLowerCase()}${cls ? '.' + cls : ''}`
      + `${tid ? `[data-testid=${tid}]` : ''} @${Math.round(r.left)},${Math.round(r.top + scrollY)} ${Math.round(r.width)}x${Math.round(r.height)}`
      + `${own ? '  “' + own + '”' : ''}`);
    for (const c of el.children) walk(c, depth + 1);
  };
  walk(document.querySelector(root) || document.body, 0);
  return out.join('\n');
}
