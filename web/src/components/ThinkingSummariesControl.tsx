import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { getThinkingSummaries, saveThinkingSummaries } from "@/lib/thinkingSummariesApi";
import { SettingsEntryLabel, SettingsItem } from "@/pages/settings/search/SettingsItem";
import { THINKING_SUMMARIES_ENTRY } from "@/pages/settings/search/catalog/general";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";

type LoadState = "loading" | "ready" | "error";

/** Toggles the signed-in user's request for Claude and Codex thinking summaries. */
export function ThinkingSummariesControl() {
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false);
  // Guards every async state update, from the initial load, Retry and toggle.
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoadState("loading");
    try {
      const current = await getThinkingSummaries();
      if (!mountedRef.current) return;
      setEnabled(current.enabled);
      setLoadState("ready");
    } catch {
      if (mountedRef.current) setLoadState("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = useCallback(async (next: boolean) => {
    setSaving(true);
    try {
      const result = await saveThinkingSummaries(next);
      if (mountedRef.current) setEnabled(result.enabled);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      toast.error(`Couldn't save thinking summaries: ${reason}`);
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }, []);

  return (
    <SettingsItem
      entry={THINKING_SUMMARIES_ENTRY}
      className="flex flex-col gap-2"
      data-testid="thinking-summaries-control"
    >
      <div className="flex items-start justify-between gap-6">
        <SettingsEntryLabel entry={THINKING_SUMMARIES_ENTRY} className="flex-1" />
        <Switch
          {...settingsControlProps(THINKING_SUMMARIES_ENTRY)}
          checked={enabled}
          disabled={loadState !== "ready" || saving}
          onCheckedChange={(next) => void toggle(next)}
          data-testid="thinking-summaries-toggle"
          className="mt-0.5 shrink-0"
          componentId="settings.general.thinking_summaries"
        />
      </div>
      {loadState === "error" && (
        <div className="flex items-center justify-between gap-3">
          <span className="text-sm text-muted-foreground" data-testid="thinking-summaries-status">
            Couldn't load your thinking summaries setting.
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void load()}
            {...settingsControlProps(THINKING_SUMMARIES_ENTRY, "retry")}
            data-testid="thinking-summaries-retry"
            componentId="settings.general.thinking_summaries.retry"
          >
            Retry
          </Button>
        </div>
      )}
    </SettingsItem>
  );
}
