"""Engine semantic version and application build are independent identifiers."""
ENGINE_VERSION = 'v0.4-B31'
APP_BUILD = "B37.5.0.8.2-HP1"
VERSION_LABEL = f'engine {ENGINE_VERSION} / App build {APP_BUILD}'
MODULE_VERSION = f'RaidWipeReview-{ENGINE_VERSION}'
INCIDENT_SCHEMA_VERSION = 34  # unchanged: B36 adds Coiled P1/P2 mechanic analysis; B31 incident facts unchanged


def version_metadata():
    return {'engineVersion': ENGINE_VERSION, 'appBuild': APP_BUILD}
