#!/usr/bin/python

from cortex.app import CortexFlask

# initalise cortex Flask application
app = CortexFlask(__name__)

# load user lib because some decorators are defined there
import cortex.lib.user

# load cortex views modules so the decorators are processed
import cortex.views.errors
import cortex.views.admin
import cortex.views.views
import cortex.views.vmware
import cortex.views.systems
import cortex.views.puppet
import cortex.views.api
import cortex.views.register
import cortex.views.user

# load workflows - they have to be done here after app is created
app.load_workflows()
