# match-tracks

A Flask app to store coordinate paths.

#### Setup: 

1. Install mongodb: `brew tap mongodb/brew` then `brew install mongo`. ([full instructions](https://docs.mongodb.com/manual/tutorial/install-mongodb-on-os-x/))
   
2. Start `mongod`: follow directions at the end of brew's installation based on your usage (as a service, or single shot).

3. Install dependencies: `pipenv sync`

4. Run locally: `pipenv run python run.py`

#### To deploy:

match-tracks is configured to deploy via [dokku](https://github.com/dokku/dokku), a Docker container management service.

1. Add dokku remote: `git remote add dokku dokku@<service-url>:match-tracks`

2. Push the repo: `git push dokku`

3. Set any instance environment variables (like API tokens) on the **remote** service in `instance/config.py`
