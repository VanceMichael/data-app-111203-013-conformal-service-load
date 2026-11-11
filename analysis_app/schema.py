FIELDS = {'observation_id': 'str', 'service': 'str', 'tier': 'str', 'hour': 'int', 'load': 'float', 'capacity': 'float', 'deploy': 'int', 'known_at': 'int'}
REQUIRED = ['observation_id', 'service', 'tier', 'hour', 'load', 'capacity', 'deploy', 'known_at']
ENTITY = 'service'
GROUP = 'tier'
MEASURE = 'load'
PRODUCT = 'conformal-service-load'
UNIT = '请求数'
