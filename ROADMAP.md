# Roadmap

Where the library is heading: what is still to be added, what should be
improved, and what is left out on purpose, with the reason.
[FEATURES.md](FEATURES.md) has the status per screen of the iTM's web UI; this
file explains the plan behind it.

The library was developed against one iTM. Anything that iTM doesn't have
(equipment, licences, dealer options) can only be built from the web UI's own
code and tested against the simulated iTM in `tests/`. Contributions from people
with other installations are the way to close those gaps; see
[How you can help](#how-you-can-help).

## Still to add

Nothing: every screen of the web UI that isn't left out on purpose (see below)
has a reader, and writes where the screen has settings. What is left is
validating on real iTMs (see "To improve").

## To improve

| What | Why |
| :--- | :-- |
| **Validate the functions tested only against the simulated iTM** (marked F in [FEATURES.md](FEATURES.md); their writes need `allow_experimental=True` until then) | Twice a real iTM behaved differently from what the web UI's code suggested: the order of a dialog, and a download's name and duration. Each live test so far either confirmed a function or found a fix. Open, with what each needs: the logged-in user's own language and formats (reading, the icon colour and the password are validated), setting a normal user's allowed screens (graphical screens set up), backup (it starts a job at once), the energy navigator's planned and actual consumption, sampling and calendar copy (best with the licence), emergency stop management (the "emergency stop" dealer option) and release (an active stop), the unit settings dialog's optional settings (equipment with hot water, outputs, low noise or a capacity limit), the °F and decimal comma formats, PPD day settings, the filter and dust alarm resets; the dealer-option functions' writes and demand control (the dealer options switched on); the leakage check (outdoor units registered for it, and a 3-hour window); R32 output ports, layout setup and the energy navigator's installer settings (service changes); the password setting; zone member order with Jump. |
| **History export codes** | The parser names the event, source and value codes seen so far (power, setpoint, fan speed, logins; schedule, overtime timer, temperature limit). Other codes, such as the auto changeover's, are kept as numbers until an export shows them next to the history screen. |
| **Fewer logins for polling clients** | The iTM allows one session, so every call logs in and out, and each login and logout is a history entry. A client that reads everything it needs in one call per poll (as the Home Assistant integration does) is already at the minimum; fewer logins means polling less often, or keeping a session open, which locks every other user (the browser too) out while it lasts. |
| **Language coverage of the newer screens** | The dealer-option, demand, service and energy navigator screens were built from the Dutch labels and the web UI's code; where they read text (activation, options), other display languages are not verified yet. |
| **Language coverage** | Labels are verified for nine display languages. A few are inferred (some dry mode labels, a non-admin user type, Korean). |

## Left out on purpose

These can be added, but each needs a good reason and care. Read the reason before
starting.

| What | Why it is left out | What it would take |
| :--- | :----------------- | :----------------- |
| **Service writes**: time zone, dealer options, licence activation, changeover option, contact | Installer settings behind the service password. A wrong value can make the iTM unusable or need a reboot, and dealer options change what the iTM does. These service screens are read only; the service writes that are built (demand pulse and ports, R32 output ports, layout setup, the energy navigator's installer settings, disabling energy saving, the leakage check) each need `allow_service_change` or their own guard. | A clear use case, a guard flag per write, and tests on a device that may be reconfigured. |
| **Destructive service functions**: bus scan (A/C auto registration), DIII-NET management, history deletion | They re-register or remove equipment or data on the device. A bus scan can change which units the iTM knows. | Probably never as library calls; at most a documented, guarded expert function. |
| **Point data registration** (about 27 installer screens) | The installer's registration of management points: how the iTM addresses each unit. Wrong data breaks control of the units. | An installer use case and a test device. |
| **Starting an emergency stop** | The web UI has no way to start one; only the iTM's input can. Releasing and managing programs are built. | Nothing: this stays out. |
| **Network changes without a guard**: IP settings, web server ports, remote management | A wrong value locks every client out of the iTM until someone is at the panel. These writes exist, but only behind `allow_network_change` / `allow_remote_change`, and were validated live as a dry run. | Stays guarded. |
| **Touch panel calibration** | Needs someone touching the panel. | Not possible from the web UI. |
| **The graphical layout view** | It shows a floor plan only after an installer has set one up in the service screens (layout setup). | An iTM with a layout. |
| **Live writes of the dealer-option functions**: interlock, temperature limit, sliding temperature, heating mode optimisation, demand control | Built: readers validated on the empty screens, writes tested against the simulated iTM. They have not been validated with the options switched on; dealer options need no key (an installer switches them on in the service screens, then the iTM reboots), but whether the functions act while switched off is unknown. Demand control also needs a pulse input. | Switching the options on (a reboot) and testing on a throwaway group or program. |
| **Paid licences**: PPD results, energy navigator data | These need an activation key (MAC address plus software ID), and none was available for development. | A contributor with a licensed iTM. |

## How you can help

The most useful contributions come from installations that differ from the one
the library was tested on (firmware Ver1.32.00): another iTM model or firmware
version, other equipment (ventilation units that report their mode and air
volume, hot water, reheat, output points), licences or dealer options, or another
display language.

1. **Open an issue first** with what your iTM has and what you'd like to add
   (see [CONTRIBUTING.md](CONTRIBUTING.md)).
2. **Capture what the web UI does.** The web UI talks to the iTM with
   `POST /cmd/` requests. Record them with the browser's developer tools (network
   tab, saved as HAR) while you use the function, or with `itm --trace FILE ...`,
   which writes each request's numbers (never its text, so not the login) and
   each reply with its session token blanked.
3. **Remove your installation's values before sharing anything**: names,
   descriptions, addresses, IP and MAC addresses, e-mail addresses, credentials,
   session tokens, readings, schedules and captured dates. Replace them with
   invented values (and correct each string's length prefix), so the replies
   still decode. Never share an export, backup or history archive as is: they
   contain the iTM's setup and authentication data.
4. **Build against the simulated iTM** (`tests/fake_itm.py` and the fakes next to
   it), so the function is tested without a device, and mark it in
   [FEATURES.md](FEATURES.md) as validated on a real iTM only once it was.

Writes on a real iTM change a live installation. Test them when nobody depends
on it, with a change you can undo, and read the result back in a new session,
as the library does for every write.
