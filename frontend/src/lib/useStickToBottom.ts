import { useCallback, useEffect, useRef, useState } from "react";

/** How close to the bottom (px) still counts as "at the bottom". */
const THRESHOLD_PX = 48;

const jumpToEnd = (el: HTMLElement) => el.scrollTo({ top: el.scrollHeight, behavior: "instant" });

/** Whether a scroll position counts as "at the bottom" (within `THRESHOLD_PX`). */
export const isNearBottom = (box: { scrollHeight: number; scrollTop: number; clientHeight: number }) =>
  box.scrollHeight - box.scrollTop - box.clientHeight <= THRESHOLD_PX;

/** Whether a thread whose message count went `previous` → `count` re-attaches:
 *  the reader added a turn, or it filled from empty (a restored conversation). */
export const followsOwnTurn = (previous: number, count: number, lastIsOwn: boolean) =>
  (count > previous && lastIsOwn) || (previous === 0 && count > 0);

/**
 * Follow a streaming thread only while the reader is at its bottom.
 *
 * While the reader sits at (or near) the bottom, every change inside the scroll
 * container — a streamed delta, a tool card, a highlighted code block, an image
 * load — keeps it there. Once they scroll up to read, new output no longer drags
 * them down; `atBottom` turns false so the thread can offer a way back
 * (`JumpToLatest`). `toBottom()` jumps and re-attaches — call it when the reader
 * sends a message, so their own turn is always in view.
 *
 * Following uses an instant scroll on purpose — `behavior: "instant"` overrides a
 * container's CSS `scroll-behavior: smooth` too: a smooth scroll emits intermediate
 * scroll events that would read as the reader leaving the bottom.
 */
export function useStickToBottom<T extends HTMLElement = HTMLDivElement>() {
  const [el, setEl] = useState<T | null>(null);
  const pinned = useRef(true);
  // where the last follow left the thread: growing content never lowers scrollTop,
  // so a lower value means the reader scrolled up — even before the browser has
  // dispatched the scroll event (a streamed delta can land in between)
  const followedTo = useRef<number | null>(null);
  const [atBottom, setAtBottom] = useState(true);

  const toBottom = useCallback(() => {
    pinned.current = true;
    setAtBottom(true);
    if (el) {
      jumpToEnd(el);
      followedTo.current = el.scrollTop;
    }
  }, [el]);

  useEffect(() => {
    if (!el) return;
    const follow = () => {
      if (!pinned.current) return;
      // a shrink (a spinner or the jump pill going away) clamps scrollTop down too,
      // but leaves the thread at its bottom — that is not the reader leaving
      if (followedTo.current !== null && el.scrollTop < followedTo.current - 1 && !isNearBottom(el)) {
        pinned.current = false;
        setAtBottom(false);
        return;
      }
      jumpToEnd(el);
      followedTo.current = el.scrollTop;
    };
    const onScroll = () => {
      const near = isNearBottom(el);
      pinned.current = near;
      followedTo.current = near ? el.scrollTop : null;
      setAtBottom(near);
    };
    // content changes (streamed text, new turns, async renders) and container resizes
    const mutations = new MutationObserver(follow);
    mutations.observe(el, { childList: true, subtree: true, characterData: true });
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(follow);
    resize?.observe(el);
    el.addEventListener("scroll", onScroll, { passive: true });
    follow();
    return () => {
      mutations.disconnect();
      resize?.disconnect();
      el.removeEventListener("scroll", onScroll);
    };
  }, [el]);

  return { ref: setEl, atBottom, toBottom };
}

/**
 * Re-attach a thread to its bottom when the reader adds a turn, or when a thread
 * fills from empty (a restored conversation): for threads that only receive the
 * message list, not the send action.
 */
export function useFollowOwnTurns(count: number, lastIsOwn: boolean, toBottom: () => void) {
  const previous = useRef(count);
  useEffect(() => {
    const follow = followsOwnTurn(previous.current, count, lastIsOwn);
    previous.current = count;
    if (follow) toBottom();
  }, [count, lastIsOwn, toBottom]);
}
