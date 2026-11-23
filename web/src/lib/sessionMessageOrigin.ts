// ``sys_session_message`` prefixes each delivered message with its sender's id
// (``orchestration_message`` in omnigent/server/session_orchestration.py).
const SESSION_MESSAGE_PREFIX_RE = /^\[Message from session ([A-Za-z0-9_-]+)\]\r?\n/;

// A sessionless sender (a supervisor, a relay) names itself: ``[Message from <label>]``.
const NAMED_MESSAGE_PREFIX_RE = /^\[Message from (?!session )([^\][\r\n]{1,64})\]\r?\n/;

/**
 * A message another sender sent, split into its sender and its text. A session sender has
 * ``sourceId``; a sessionless sender has only ``label``.
 */
export interface SessionMessageOrigin {
  sourceId: string | null;
  label: string | null;
  body: string;
}

/**
 * Split the sender prefix off a message delivered by ``sys_session_message``.
 *
 * The prefix is a display label written by the server, not proof of origin.
 *
 * :param text: Message text, e.g. ``"[Message from session 9b8b…]\nhi"``.
 * :returns: ``{sourceId: "9b8b…", label: null, body: "hi"}``, ``{sourceId: null, label:
 *   "Supervisor", body}`` for ``"[Message from Supervisor]\n…"``, or ``null`` without a prefix.
 */
export function splitSessionMessageOrigin(text: string): SessionMessageOrigin | null {
  const match = SESSION_MESSAGE_PREFIX_RE.exec(text);
  if (match) return { sourceId: match[1], label: null, body: text.slice(match[0].length) };
  const named = NAMED_MESSAGE_PREFIX_RE.exec(text);
  const label = named?.[1].trim();
  if (!named || !label) return null;
  return { sourceId: null, label, body: text.slice(named[0].length) };
}
