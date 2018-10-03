# match-tracks

A Flask app to store coordinate paths.

####Setup: 

1. Install mongodb: `brew install mongodb`

2. Install dependencies: `pipenv install`

3. Run locally: `pipenv run python run.py`

####To deploy:

match-tracks is configured to deploy via [dokku](https://github.com/dokku/dokku), a Docker container management service.

1. Add dokku remote: `git remote add dokku dokku@<service-url>:match-tracks`

2. Push the repo: `git push dokku`

3. Set any instance environment variables on the **remote** service in `instance/config.py`

