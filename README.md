# OpenAVC Community Driver Library

Device drivers for the [OpenAVC](https://github.com/open-avc/openavc) open-source AV control platform.

## What's Here

This repository contains community-maintained device drivers for AV equipment — projectors, displays, switchers, DSPs, cameras, lighting controllers, and more. Drivers are installed directly from the OpenAVC Programmer IDE.

## Driver Formats

| Format | Extension | Use Case |
|--------|-----------|----------|
| **YAML definition** | `.avcdriver` | Text-based protocols (Extron SIS, Kramer, Biamp, Shure, etc.). No code required — just define commands, responses, and polling. |
| **Python driver** | `.py` | Complex protocols requiring binary framing, checksums, multi-step auth, or external libraries (Samsung MDC, VISCA, etc.). |

Most AV protocols are text-based command/response and work great as `.avcdriver` YAML files. Python drivers are only needed when the protocol requires logic that can't be expressed declaratively.

## Installing Drivers

In the OpenAVC Programmer IDE:

1. Go to the **Drivers** view
2. Click **Browse Drivers**
3. Search or filter by category/manufacturer
4. Click **Install**

Or import a driver file manually via **Import** in the Driver Builder.

## Directory Structure

```
projectors/          # Projector control (PJLink, Sony ADCP, etc.)
displays/            # Commercial displays (Samsung, LG, NEC, Sony)
switchers/           # Matrix switchers, presentation switchers, scalers
audio/               # DSPs, mixers, amplifiers, microphones
video/               # Video production software (vMix, OBS, Wirecast, TriCaster)
cameras/             # PTZ cameras (VISCA, Panasonic AW, etc.)
lighting/            # DMX, Art-Net, sACN, architectural lighting
utility/             # Wake-on-LAN, relays, generic TCP/serial, bridges
index.json           # Driver catalog (used by the Browse Drivers UI)
```

## Contributing a Driver

1. Create your driver using the **Driver Builder** in the Programmer IDE (exports `.avcdriver`) or write a Python driver
2. Test it against real hardware or a simulator
3. Fork this repo, add your driver to the appropriate category folder
4. Update `index.json` with your driver's metadata
5. Submit a pull request

See the [Contributing Guide](docs/contributing-drivers.md) for the full checklist, and the [Driver Creation Guide](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) in the main repo for complete documentation on YAML and Python driver formats.

## License

All drivers in this repository are released under the [MIT License](LICENSE). By contributing, you agree to license your driver under MIT.
