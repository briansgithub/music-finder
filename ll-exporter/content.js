(async () => {
  if (window.__llExportRunning) return; window.__llExportRunning = true;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const banner = document.createElement("div");
  banner.style.cssText = "position:fixed;top:60px;right:20px;z-index:99999;background:#222;color:#fff;padding:12px 16px;border-radius:8px;font:14px sans-serif;box-shadow:0 2px 8px #0008";
  document.body.appendChild(banner);
  // Supports both the new layout (yt-lockup-view-model) and the old one (ytd-playlist-video-renderer)
  const SEL = "ytd-playlist-video-renderer, ytd-item-section-renderer yt-lockup-view-model, ytd-playlist-video-list-renderer yt-lockup-view-model, ytd-section-list-renderer yt-lockup-view-model";
  const rows = () => {
    let r = document.querySelectorAll(SEL);
    if (!r.length) r = document.querySelectorAll("ytd-browse yt-lockup-view-model");
    return r;
  };
  let last = -1, stable = 0;
  while (stable < 6) {
    const n = rows().length;
    banner.textContent = `Loading liked videos... ${n} so far (leave this tab open)`;
    if (n === last) stable++; else { stable = 0; last = n; }
    const cont = document.querySelector("ytd-continuation-item-renderer");
    if (cont) cont.scrollIntoView({block: "end"});
    window.scrollTo(0, document.documentElement.scrollHeight);
    await sleep(1200 + Math.random() * 800);
  }
  const txt = el => (el?.textContent || "").trim();
  const vid = href => ((href || "").match(/[?&]v=([\w-]{11})/) || [])[1] || "";
  const data = [...rows()].map((v, i) => {
    let title, href, channel, channelUrl, duration, durationLabel;
    if (v.tagName.toLowerCase() === "yt-lockup-view-model") {
      const a = v.querySelector("a.ytLockupMetadataViewModelTitle") || v.querySelector("h3 a") || v.querySelector("a[href*='watch?v=']");
      href = a?.href;
      title = v.querySelector("h3")?.getAttribute("title") || txt(a);
      const ch = v.querySelector(".ytContentMetadataViewModelMetadataRow a[href*='/@'], .ytContentMetadataViewModelMetadataRow a[href*='/channel/']");
      channel = txt(ch) || txt(v.querySelector(".ytContentMetadataViewModelMetadataRow span"));
      channelUrl = ch?.href || "";
      const b = v.querySelector("badge-shape");
      duration = txt(b?.querySelector(".ytBadgeShapeText") || b);
      durationLabel = b?.getAttribute("aria-label") || "";
    } else {
      const a = v.querySelector("a#video-title");
      href = a?.href; title = a?.getAttribute("title") || txt(a);
      const ch = v.querySelector("ytd-channel-name a");
      channel = txt(ch); channelUrl = ch?.href || "";
      duration = txt(v.querySelector("badge-shape, #time-status #text"));
    }
    if (!href) { const m = (v.innerHTML.match(/content-id-([\w-]{11})/) || [])[1]; if (m) href = "https://www.youtube.com/watch?v=" + m; }
    const id = vid(href);
    const hms = (duration.match(/^\d+(:\d+)+$/) ? duration.split(":").reduce((s, x) => s * 60 + +x, 0) : null);
    return {
      index: i + 1, video_id: id,
      url: id ? `https://www.youtube.com/watch?v=${id}` : (href || ""),
      title: title || "", channel: channel || "", channel_url: channelUrl || "",
      duration, duration_seconds: hms, duration_label: durationLabel || "",
      unavailable: !id || /^\[(private|deleted)/i.test(title || "")
    };
  }).filter(d => d.video_id || d.title);
  const out = {exported_at: new Date().toISOString(), source: location.href, count: data.length, videos: data};
  const blob = new Blob([JSON.stringify(out, null, 2)], {type: "application/json"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `liked_LL_${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(link); link.click(); link.remove();
  banner.textContent = data.length ? `Done: saved ${data.length} videos to your Downloads folder.` : "No videos found on this page. Please send the page to Music Finder.";
  setTimeout(() => banner.remove(), 10000);
  window.__llExportRunning = false;
})();
