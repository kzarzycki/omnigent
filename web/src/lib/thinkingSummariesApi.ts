// Typed client for the caller's thinking-summaries setting
// (`omnigent/server/routes/user_settings.py`). Sessions the caller starts ask
// Claude and Codex for readable thinking when it is on.

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

const THINKING_SUMMARIES_PATH = "/v1/settings/thinking-summaries";

export interface ThinkingSummariesSetting {
  /** Whether sessions the caller starts ask for thinking summaries. */
  enabled: boolean;
}

export async function getThinkingSummaries(): Promise<ThinkingSummariesSetting> {
  const res = await authenticatedFetch(THINKING_SUMMARIES_PATH);
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ThinkingSummariesSetting;
}

export async function saveThinkingSummaries(enabled: boolean): Promise<ThinkingSummariesSetting> {
  const res = await authenticatedFetch(THINKING_SUMMARIES_PATH, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ThinkingSummariesSetting;
}
