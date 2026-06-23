# UI Design Spec — GHKGE Admin Console
**Document Version:** 1.0  
**Status:** Approved  
**Target Audience:** Frontend Developers, UI/UX Designers.

---

## 1. Design System & Aesthetics
The GHKGE Admin Console must look premium, modern, and highly interactive. It uses a custom **Glassmorphism Dark Theme** with Outfit and Inter typography.

### 1.1 CSS Utility Tokens (`index.css`)
```css
:root {
  --bg-dark: #090a0f;
  --bg-card: rgba(17, 19, 31, 0.65);
  --border-glass: rgba(255, 255, 255, 0.08);
  
  --primary-glow: #6366f1;     /* Indigo */
  --secondary-glow: #3b82f6;   /* Blue */
  --accent-neon: #10b981;      /* Emerald (Active) */
  --warning-neon: #f59e0b;     /* Amber (Pending) */
  --danger-neon: #ef4444;      /* Red (Blocked) */
  
  --text-primary: #f3f4f6;
  --text-secondary: #9ca3af;
}

body {
  background-color: var(--bg-dark);
  font-family: 'Outfit', system-ui, sans-serif;
  color: var(--text-primary);
  margin: 0;
  padding: 0;
}
```

---

## 2. Admin Dashboard Layout
The interface is structured as an interactive single-page application dashboard split into four primary views:

```
+-------------------------------------------------------+
|  GHKGE Admin Console              [System Status: OK] |
+-------------------------------+-----------------------+
|  Active Runs                  |  Coverage Gap Queue   |
|  - Run #8fa27 (Landmarks) 85%  |  - tdr1v2 [Landmarks] |
|  - Run #2a391 (Events) 100%   |  - tdr1v3 [Businesses]|
+-------------------------------+-----------------------+
|  Strategy Yield Metrics       |  Pending Conflict     |
|  - OSM Api: 14.2 entities/run |  Resolution Queue     |
|  - Reddit Scraper: 2.1 ent    |  - "City Park" (Diff) |
+-------------------------------+-----------------------+
```

---

## 3. UI Component Specs

### 3.1 Active Runs Panel
*   Displays real-time lists of execution states.
*   *Interaction:* Click a run to view real-time log outputs, run origins (manual vs scheduled), and active chromium processes.
*   *Aesthetics:* Uses animated progress bars with gradient fills matching `--primary-glow` and `--secondary-glow`.

### 3.2 Coverage Gap Queue Table
*   Displays prioritized cells needing additional crawls.
*   *Columns:* Cell ID, Entity Type, Gap Severity score, Status, Quick Actions.
*   *Interaction:* "Trigger Scrape" button calls `POST /v1/runs` immediately with custom parameters. "Resolve" button calls the override endpoint.

### 3.3 Conflict Resolution Interface (Diff View)
*   Triggered when the system flags contradicting facts of equal source-tier status.
*   **Split Screen Layout:**
    *   *Left Column (Existing Node Details):* Displays existing DB fields, values, confidence ratings, and source urls.
    *   *Right Column (Incoming Fact Details):* Displays incoming schema fields.
    *   *Control Buttons:* "Override Existing," "Append as Alias," or "Discard Incoming."
*   **Interaction Design:** Smooth CSS hover translations (`transform: translateY(-2px)`) and active glowing borders when cards are selected.
