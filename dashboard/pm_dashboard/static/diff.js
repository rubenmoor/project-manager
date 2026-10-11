// The commit `pm run` made for a task, as a syntax-highlighted diff (diff2html, loaded on first use).
import { html, useState, useEffect, useRef } from "/vendor/preact-htm.js";
import { stamp } from "/data.js";

let loading = null;
function loadDiff2html() {
  if (!loading) loading = new Promise((ok, fail) => {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "/vendor/diff2html.css";
    document.head.append(css);
    const s = document.createElement("script");
    s.src = "/vendor/diff2html-ui.js";
    s.onload = () => ok(window.Diff2HtmlUI);
    s.onerror = fail;
    document.head.append(s);
  });
  return loading;
}

const dark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
};

export function TaskDiff({ task, version }) {
  const [c, setC] = useState(null);
  const [side, setSide] = useState(false);
  const box = useRef(null);
  useEffect(() => {
    let gone = false;
    fetch(`/api/task/${task.id}/commit`).then((r) => r.json()).then((j) => !gone && setC(j));
    return () => { gone = true; };
  }, [task.id, version]);
  useEffect(() => {
    if (!c || !c.patch || !box.current) return;
    loadDiff2html().then((UI) => {
      if (!box.current) return;
      const ui = new UI(box.current, c.patch, {
        drawFileList: true, fileListToggle: true, fileListStartVisible: false, fileContentToggle: true, matching: "lines",
        outputFormat: side ? "side-by-side" : "line-by-line", highlight: true, synchronisedScroll: true,
        colorScheme: dark() ? "dark" : "light",
      });
      ui.draw();
      ui.highlightCode();
    });
  }, [c, side]);
  if (!c) return html`<div class="empty">Loading the commit…</div>`;
  if (!c.commit) return html`<div class="empty">No commit for this task yet${task.status === "open" ? "" : " (closed outside `pm run`)"}.</div>`;
  const add = c.files.reduce((s, f) => s + (f.added || 0), 0), del = c.files.reduce((s, f) => s + (f.deleted || 0), 0);
  return html`<div class="row sub" style="margin-bottom:8px">
      <span class="mono">${c.commit.slice(0, 7)}</span><span>${stamp(c.date)}</span>
      <span>${c.files.length} files, <span class="plus">+${add}</span> <span class="minus">−${del}</span></span>
      <span class="grow"></span>
      <button class="iconbtn" onClick=${() => setSide(!side)}>${side ? "unified" : "side by side"}</button></div>
    ${c.truncated ? html`<div class="banner warn">The patch is large; it is cut off.</div>` : null}
    <div class="diffbox" ref=${box}></div>`;
}
