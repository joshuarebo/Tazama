# Email to send with the demo attachment

Attach: `docs/demo/tazama-demo.html`

---

**Subject:** TAZAMA — interactive demo attached

Hi Andrew,

Attached is a single HTML file — download it and open it in any browser. Nothing to install,
and it works offline.

It's TAZAMA end to end: it connects what the network already knows to what customers are
already saying, so we find out about a fault from our own data instead of from the queue at
the contact centre. Click through the tabs — the first one is a short write-up on the
problem, how it works, and what we'd need to run it on live data.

One thing worth flagging up front: everything in it is synthetic demo data. The
architecture, the pipeline and the AI are built and tested — what's left is pointing it at
real tables.

Happy to walk the network team through it whenever suits.

Joshua

---

## If you want a shorter version

**Subject:** TAZAMA — interactive demo attached

Hi Andrew,

Attached is a single HTML file — open it in any browser, works offline, nothing to install.

It's the whole platform end to end: TAZAMA connects what the network already knows to what
customers are already saying, so we hear about a fault from our own data rather than from
the contact centre queue. The first tab explains the problem, how it works and what we'd
need to go live. All the data in it is synthetic.

Happy to walk the network team through it whenever suits.

Joshua

---

## Notes for you, not for the email

- **Do not** say the accuracy figures predict live performance. They measure the mechanism on
  labelled synthetic cases. If asked, that distinction is in the "What this is" tab under
  *What is honest about the current state*.
- **The lead-lag tab finds nothing** on this data and says so. That is deliberate and it is
  worth pointing at — it shows the system refuses to overclaim. Do not describe it as
  proving the network warns us hours ahead; on this data it does not.
- **If he asks about predictive maintenance:** the three scenarios are on the "Going live"
  tab. The honest line is that today the product detects a fault early; predicting one needs
  telemetry we do not currently ingest, and the first step is the site power feed.
- **If he asks how long to go live:** the engineering is configuration, not construction —
  but three privacy procedures (erasure, salt rotation, an exposure floor on insights) have
  to be done before any real subscriber record enters the system.
