import { useCallback, useLayoutEffect, useRef, useState } from "react";

const bottomThreshold = 80;

export function useConversationScroll(followRequest: number) {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const previousTop = useRef(0);
  const [awayFromLatest, setAwayFromLatest] = useState(false);

  const scrollToLatest = useCallback(() => {
    following.current = true;
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
  }, []);

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
        else
          setAwayFromLatest(
            element.scrollHeight - element.scrollTop - element.clientHeight >
              bottomThreshold,
          );
      });
    });
    observer.observe(element);
    observer.observe(messages);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [scrollToLatest]);

  return { viewport, content, onScroll, scrollToLatest, awayFromLatest };
}
