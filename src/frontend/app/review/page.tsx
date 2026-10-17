"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** The page moved to /recommendations. A static export has no server redirects, so this
 * stub sends in-app navigation and old bookmarks there. */
export default function ReviewRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/recommendations");
  }, [router]);
  return <p role="status">Moving to Recommendations…</p>;
}
