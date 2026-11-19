"""Knowledge package data flywheel: evaluation feedback, assistant optimization and Hub release.

Every user of a project keeps an own flywheel, because evaluations and Hub packages are private to
each user. Reading evaluation artifacts and publishing need that user's authority, and the user's login
is never stored. While the user is present the steps use the login of their requests; turning a switch
on registers a delegation with the service concerned, so background rounds carry on while the user is
away (see grants and background).
"""
