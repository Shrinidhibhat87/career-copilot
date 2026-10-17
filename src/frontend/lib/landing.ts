import { getResume } from "@/lib/api";

export type Landing = "/recommendations" | "/resume";

/** A returning user with a CV opens on the recommendations; anyone else starts at the
 * resume. Any failure other than "no CV stored" throws, so the caller can say so. */
export async function routeAfterSignIn(): Promise<Landing> {
  return (await getResume()) === null ? "/resume" : "/recommendations";
}
