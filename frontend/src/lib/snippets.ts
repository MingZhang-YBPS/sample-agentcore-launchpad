/**
 * Copy-ready `/v1` integration snippets for one agent (T17).
 *
 * Generated client-side on purpose: the inputs are the agent id and the origin the
 * console is served from, both already in the browser, and the output is static text.
 * A server route would add an endpoint to classify and keep in step with `public_api.py`
 * for no gain. The contract the snippets mirror: `POST /v1/agents/{id}/invoke` returns
 * `{agent, text, session_id, latency_ms}`; `.../invoke-stream` returns SSE with `meta`,
 * `delta` (`{text}`), `error` and `done` events. Auth is the `X-Api-Key` header.
 */

export type SnippetLang = "curl" | "python" | "javascript";
export type SnippetMode = "sync" | "stream";

export const KEY_PLACEHOLDER = "YOUR_API_KEY";

export interface SnippetInput {
  /** origin serving `/v1`, no trailing slash */
  baseUrl: string;
  agentId: string;
  /** placeholder shown for the key (never a real key) */
  key?: string;
}

export const SNIPPET_LANGS: SnippetLang[] = ["curl", "python", "javascript"];
export const SNIPPET_MODES: SnippetMode[] = ["sync", "stream"];

function urlFor({ baseUrl, agentId }: SnippetInput, mode: SnippetMode): string {
  const root = baseUrl.replace(/\/+$/, "");
  return `${root}/v1/agents/${encodeURIComponent(agentId)}/${mode === "sync" ? "invoke" : "invoke-stream"}`;
}

export function buildSnippet(input: SnippetInput, lang: SnippetLang, mode: SnippetMode): string {
  const url = urlFor(input, mode);
  const key = input.key ?? KEY_PLACEHOLDER;
  if (lang === "curl") {
    return [
      `curl ${mode === "stream" ? "-N " : ""}-X POST "${url}" \\`,
      `  -H "X-Api-Key: ${key}" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d '{"prompt": "Hello!"}'`,
    ].join("\n");
  }
  if (lang === "python") {
    if (mode === "sync") {
      return [
        "import httpx",
        "",
        `resp = httpx.post(`,
        `    "${url}",`,
        `    headers={"X-Api-Key": "${key}"},`,
        `    json={"prompt": "Hello!"},`,
        `    timeout=300,`,
        `)`,
        `resp.raise_for_status()`,
        `print(resp.json()["text"])`,
      ].join("\n");
    }
    return [
      "import json",
      "import httpx",
      "",
      `with httpx.stream(`,
      `    "POST",`,
      `    "${url}",`,
      `    headers={"X-Api-Key": "${key}"},`,
      `    json={"prompt": "Hello!"},`,
      `    timeout=None,`,
      `) as resp:`,
      `    resp.raise_for_status()`,
      `    event = None`,
      `    for line in resp.iter_lines():`,
      `        if line.startswith("event: "):`,
      `            event = line[len("event: "):]`,
      `        elif line.startswith("data: ") and event == "delta":`,
      `            print(json.loads(line[len("data: "):])["text"], end="", flush=True)`,
    ].join("\n");
  }
  if (mode === "sync") {
    return [
      `const resp = await fetch("${url}", {`,
      `  method: "POST",`,
      `  headers: { "X-Api-Key": "${key}", "Content-Type": "application/json" },`,
      `  body: JSON.stringify({ prompt: "Hello!" }),`,
      `});`,
      `if (!resp.ok) throw new Error(await resp.text());`,
      `const { text } = await resp.json();`,
      `console.log(text);`,
    ].join("\n");
  }
  return [
    `const resp = await fetch("${url}", {`,
    `  method: "POST",`,
    `  headers: { "X-Api-Key": "${key}", "Content-Type": "application/json" },`,
    `  body: JSON.stringify({ prompt: "Hello!" }),`,
    `});`,
    `if (!resp.ok) throw new Error(await resp.text());`,
    ``,
    `const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();`,
    `let buffer = "";`,
    `for (;;) {`,
    `  const { value, done } = await reader.read();`,
    `  if (done) break;`,
    `  buffer += value;`,
    `  const frames = buffer.split("\\n\\n");`,
    `  buffer = frames.pop() ?? "";`,
    `  for (const frame of frames) {`,
    `    if (!frame.startsWith("event: delta")) continue;`,
    `    const data = frame.split("\\n").find((l) => l.startsWith("data: "));`,
    `    if (data) process.stdout.write(JSON.parse(data.slice(6)).text);`,
    `  }`,
    `}`,
  ].join("\n");
}
