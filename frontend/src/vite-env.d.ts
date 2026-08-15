/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

// No build-time `VITE_*` variables are read by the SPA: every runtime value it
// needs arrives from the backend (`/auth/config`, `/runtime/limits`, …), so the
// same built bundle serves self-host and hosted alike. Re-declare
// `ImportMetaEnv` here if that ever stops being true.
