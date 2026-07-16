export function isAuthorized(request, apiToken) {
  if (!apiToken) {
    return true;
  }
  const header = request.headers.authorization ?? "";
  const expected = `Bearer ${apiToken}`;
  return header === expected;
}

export function requireAuthorized(request, response, apiToken) {
  if (isAuthorized(request, apiToken)) {
    return true;
  }
  response.writeHead(401, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify({ ok: false, error: "unauthorized" }));
  return false;
}
