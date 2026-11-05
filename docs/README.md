<<<<<<< HEAD
# About my fork

## How to update ```dvmnnthonyy/dvmn-agent:main``` from ```The-PR-Agent/dvmn-agent:main```


```
chmod +x scripts/sync-with-upstream.sh
./scripts/sync-with-upstream.sh
```

 ## implement my personal feature


```
# Create branch for my features
git checkout -b feature/my-customizations origin/prod

# Edit whatever you want

# Commit this change
git add .
git commit -m "feat: amazing feature"

# push to fork
git push origin feature/my-customizations

# Merge on prod
git checkout prod
git merge feature/my-customizations
git push origin prod
```


## how to run

```
python3 -m venv .venv # Or python for other
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
```


## How to execute test 

```
PYTHONPATH=. pytest tests/unittest -q
# ou un test ciblé
PYTHONPATH=. pytest tests/unittest/test_example.py -q
```
=======
# [Visit Our Docs Portal](https://docs.pr-agent.ai/)
>>>>>>> upstream/main
