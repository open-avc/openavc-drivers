## Long poll answers from a DM-NVX-D30, 2026-10-11

Crestron DM-NVX-D30 (decoder), firmware 7.0.5057.00023, one session signed in
with `crestron_nvx`'s own sign-in form. Each file is the body of one
`GET /Device/Longpoll` answer, byte-exact (every answer was `200`,
`Content-Type: text/plain`, ending in CRLF):

- `longpoll_timeout.txt`: the answer when nothing changed, 30 s after the long
  poll was opened
- `longpoll_leds_off.txt`: `LedsEnabled` set to false by a POST to `/Device`
- `longpoll_video_source.txt`: `VideoSource` set to `Stream`
- `longpoll_active_sources.txt`: the next answer, 0.1 s later, with the values
  that followed from it
- `longpoll_scaler_resolution.txt`: the output port's `Resolution` set to
  `1920x1080@60` (a change inside an array arrives as a one-element array
  holding only the changed property)
- `longpoll_stream_route.txt` and `longpoll_stream_connecting.txt`: the two
  answers after stream 0's `StreamLocation` and `Start` were set; the unit has
  four streams, and the element names neither its index nor an id

Not a file: on the same unit a request without a live session (after
`/logout`, or carrying the ended session's cookies) was answered
`301`, `Location: /userlogin.html`, empty body, for a GET of
an object, the long poll and a POST alike.
