export default {
  async fetch(request, env) {
    const res = await env.ASSETS.fetch(request);
    const range = request.headers.get("Range");
    const url = new URL(request.url);
    if (!url.pathname.endsWith(".mp4") || res.status !== 200) return res;
    const h = new Headers(res.headers);
    h.set("Accept-Ranges", "bytes");
    const m = range && /^bytes=(\d*)-(\d*)$/.exec(range.trim());
    if (!m) return new Response(res.body, { status: 200, headers: h });
    const buf = await res.arrayBuffer();
    const size = buf.byteLength;
    let start, end;
    if (m[1] === "") { const n = Number(m[2]); start = Math.max(0, size - n); end = size - 1; }
    else { start = Number(m[1]); end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1); }
    if (!(start <= end) || start >= size) { h.set("Content-Range", `bytes */${size}`); h.delete("Content-Length"); return new Response(null, { status: 416, headers: h }); }
    h.set("Content-Range", `bytes ${start}-${end}/${size}`);
    h.set("Content-Length", String(end - start + 1));
    return new Response(request.method === "HEAD" ? null : buf.slice(start, end + 1), { status: 206, headers: h });
  },
};
