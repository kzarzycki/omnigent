import { useEffect, useRef, useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import { copyText } from "@/lib/clipboard";

// Kept for HostUpdateBanner: upstream removed the shutdown banner (#14400) but this
// fork's host-update nudge still reuses the copy-command chip.
export function CommandCopyButton({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const resetTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (resetTimeout.current) clearTimeout(resetTimeout.current);
    };
  }, []);

  const copy = async () => {
    try {
      await copyText(command);
      setCopied(true);
      if (resetTimeout.current) clearTimeout(resetTimeout.current);
      resetTimeout.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="flex max-w-full items-center gap-1 rounded border border-border bg-background/70 px-2 py-0.5">
      <code className="min-w-0 break-all">{command}</code>
      <button
        type="button"
        aria-label={copied ? `Copied ${command}` : `Copy ${command}`}
        title={copied ? "Copied" : `Copy ${command}`}
        className="rounded p-1 hover:bg-muted focus-visible:outline focus-visible:outline-2"
        onClick={() => void copy()}
      >
        {copied ? (
          <CheckIcon className="size-3.5" aria-hidden="true" />
        ) : (
          <CopyIcon className="size-3.5" aria-hidden="true" />
        )}
      </button>
    </div>
  );
}
