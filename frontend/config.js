/* ===========================================================================
   Where the dashboard looks for the API.
   The one file you edit to point this frontend at a different backend.
   ========================================================================= */

window.STARTUPPULSE_API_BASE = "";

/*  Values:

    ""                        same origin — the default. The backend serves
                              this folder, so the page and the API share a
                              host and nothing needs configuring.

    "http://127.0.0.1:8000"   an absolute URL. Use this when you serve this
                              folder yourself (python -m http.server, Live
                              Server, a static host) while the API runs
                              elsewhere. The backend allows cross-origin
                              calls, so this is the only line you change.
*/
