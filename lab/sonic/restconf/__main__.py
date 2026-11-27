"""python3 -m restconf : lance le serveur RESTCONF (PYTHONPATH contenant ce paquet, frr_config et parsers)."""

from .server import main

raise SystemExit(main())
