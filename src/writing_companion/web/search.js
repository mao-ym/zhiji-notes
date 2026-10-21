const searchResults = document.querySelector("#search-results");

export async function searchLibrary(query, options = {}) {
  // 组装搜索参数，保持所有筛选都经由后端参数化查询处理。
  const params = new URLSearchParams();
  params.set("q", query);
  for (const kind of options.kinds || []) {
    params.append("kind", kind);
  }
  if (options.start) {
    params.set("from", options.start);
  }
  if (options.end) {
    params.set("to", options.end);
  }
  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));

  const response = await fetch(`/api/search?${params.toString()}`);
  if (!response.ok) {
    throw new Error("搜索失败");
  }
  const results = await response.json();
  results.totalCount = Number(response.headers.get("X-Total-Count") || results.length);
  return results;
}

export function renderSearchResults(results, onSelect) {
  if (!searchResults) return;
  searchResults.hidden = false;
  const resultList = document.querySelector("#search-result-list") || searchResults;
  if (resultList === searchResults) searchResults.replaceChildren();
  else resultList.replaceChildren();
  searchResults.dataset.totalCount = String(results.totalCount || results.length);
  if (!results.length) {
    resultList.textContent = "没有找到匹配内容";
    return;
  }
  for (const result of results) {
    const item = document.createElement("article");
    item.className = "search-result";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-result-button";
    const title = document.createElement("strong");
    title.textContent = result.title;
    const meta = document.createElement("small");
    meta.textContent = `${result.kind} · ${result.updated_at}`;
    const snippet = document.createElement("p");
    snippet.textContent = result.snippet;
    button.append(title, meta, snippet);
    button.addEventListener("click", () => onSelect?.(result));
    item.append(button);
    resultList.append(item);
  }
}
