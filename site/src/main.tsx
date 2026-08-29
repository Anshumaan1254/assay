import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles.css";

// Deliberately not wrapped in <StrictMode>.
//
// Every section on this page registers global scroll side effects — GSAP
// ScrollTrigger pins, a body position:fixed lock, a shared Lenis instance.
// StrictMode's development-only mount/unmount/mount would tear those down
// and rebuild them mid-measurement, and ScrollTrigger caches page extents at
// creation time, so pinned sections end up measured against a document
// height that no longer exists. The production build never double-mounts;
// this only removes a dev-only behaviour that this particular page cannot
// be made correct under.
createRoot(document.getElementById("root")!).render(<App />);
