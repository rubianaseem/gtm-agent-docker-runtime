Secret files live here and are git-ignored and docker-ignored.
Create dev values with: python tools/make_dev_secrets.py
- crm_token.txt: random token shared by the mock CRM and the worker
- anthropic_api_key.txt: empty by default (template summaries). Paste a key only if you want the optional Claude path.
