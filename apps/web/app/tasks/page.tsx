"use client";

import { useEffect } from "react";

// Scheduled work moved to Routines (docs/ROUTINES.md). Old links and bookmarks keep working.
export default function TasksRedirect() {
  useEffect(() => {
    window.location.replace(`/routines${window.location.search}`);
  }, []);
  return null;
}
