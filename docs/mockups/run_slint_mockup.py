"""Show the Slint mockup of PESForge (pip install slint first)."""
import os
import slint

ui = slint.load_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pesforge-slint.slint"))
ui.PesForge().run()
