"use client";

import { useEffect } from "react";

const isGuarded = (url: string) => url.includes("/api/") && !url.includes("/api/auth/");

/** Every screen reads the API with a plain `fetch`. When the server says nobody is signed
 * in (a restart, or a sign-out in another tab), send the browser to the sign-in page once,
 * here, rather than teaching each screen to handle a 401. */
export function AuthGuard() {
  useEffect(() => {
    const original = window.fetch;
    window.fetch = async (...args: Parameters<typeof fetch>) => {
      const response = await original(...args);
      const input = args[0];
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (response.status === 401 && isGuarded(url) && window.location.pathname !== "/") {
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination
        window.location.assign("/");
      }
      return response;
    };
    return () => {
      window.fetch = original;
    };
  }, []);
  return null;
}
