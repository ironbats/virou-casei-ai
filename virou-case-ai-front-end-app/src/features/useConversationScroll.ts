import { useCallback, useLayoutEffect, useRef, useState } from "react";

const bottomThreshold = 80;

export function useConversationScroll(followRequest: number) {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const previousTop = useRef(0);
  const anchor = useRef<{ node: Element; offset: number } | null>(null);

  const rememberAnchor = useCallback(() => {
    const element = viewport.current;
    if (!element || !element.clientHeight) return;
    const bounds = element.getBoundingClientRect();
    const node = Array.from(content.current?.children || []).find(
      (item) => item.getBoundingClientRect().bottom > bounds.top + 1,
    );
    anchor.current = node
      ? { node, offset: node.getBoundingClientRect().top - bounds.top }
      : null;
  }, []);
  const [awayFromLatest, setAwayFromLatest] = useState(false);

  const scrollToLatest = useCallback(() => {
    following.current = true;
    anchor.current = null;
    setAwayFromLatest(false);
    const element = viewport.current;
    if (!element || !element.clientHeight) return;
    element.scrollTop = element.scrollHeight;
    previousTop.current = element.scrollTop;
  }, []);

  const onScroll = useCallback(() => {
    const element = viewport.current;
    if (!element || !element.clientHeight) return;
    const nearBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight <=
      bottomThreshold;
    if (nearBottom) following.current = true;
    else if (element.scrollTop < previousTop.current - 1)
      following.current = false;
    previousTop.current = element.scrollTop;
    setAwayFromLatest(!following.current && !nearBottom);
    if (!following.current) rememberAnchor();
  }, [rememberAnchor]);

  useLayoutEffect(() => {
    scrollToLatest();
  }, [followRequest, scrollToLatest]);

  useLayoutEffect(() => {
    const element = viewport.current;
    const messages = content.current;
    if (!element || !messages) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!element.clientHeight) return;
        if (following.current) scrollToLatest();
        else {
          const saved = anchor.current;
          if (saved?.node.isConnected && messages.contains(saved.node)) {
            const displacement =
              saved.node.getBoundingClientRect().top -
              element.getBoundingClientRect().top -
              saved.offset;
            if (Math.abs(displacement) > 1) element.scrollTop += displacement;
            previousTop.current = element.scrollTop;
          }
          rememberAnchor();
          setAwayFromLatest(
            element.scrollHeight - element.scrollTop - element.clientHeight >
              bottomThreshold,
          );
        }
      });
    });
    observer.observe(element);
    observer.observe(messages);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [scrollToLatest, rememberAnchor]);

  return { viewport, content, onScroll, scrollToLatest, awayFromLatest };
}
