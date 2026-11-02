export const GITHUB_REPO = "specialshweta26/Checkme";
export const RELEASES_PAGE = `https://github.com/${GITHUB_REPO}/releases/latest`;

export type Platform = "mac" | "windows" | "other";

export function detectPlatform(): Platform {
  if (typeof navigator === "undefined") return "other";
  const ua = navigator.userAgent || "";
  const plat = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform || navigator.platform || "";
  if (/Win/i.test(plat) || /Windows/i.test(ua)) return "windows";
  if (/Mac/i.test(plat) || /Mac OS|Macintosh/i.test(ua) || /iPhone|iPad|iPod/.test(ua)) return "mac";
  return "other";
}

export type Installers = { mac?: string; windows?: string; page: string };

export async function latestInstallers(): Promise<Installers> {
  try {
    const res = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/releases/latest`, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!res.ok) return { page: RELEASES_PAGE };
    const data = await res.json();
    const assets: { name?: string; browser_download_url?: string }[] = data.assets || [];
    const pick = (test: RegExp) => assets.find((a) => test.test(a.name || ""))?.browser_download_url;
    return {
      mac: pick(/\.dmg$/i),
      windows: pick(/\.(exe|msi)$/i),
      page: data.html_url || RELEASES_PAGE,
    };
  } catch {
    return { page: RELEASES_PAGE };
  }
}
