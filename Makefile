NODE_BIN=./node_modules/.bin
DIFF_COVER_BASE_BRANCH=master
PYTHON_ENV=py38
DJANGO_ENV_VAR=$(if $(DJANGO_ENV),$(DJANGO_ENV),django32)

help:
	@echo ''
	@echo 'Makefile for the edX ecommerce project.'
	@echo ''
	@echo 'Usage:'
	@echo '    make requirements                          install requirements for local development'
	@echo '    make migrate                               apply migrations'
	@echo '    make serve                                 start the dev server at localhost:8002'
	@echo '    make clean                                 delete generated byte code and coverage reports'
	@echo '    make validate_js                           run JavaScript unit tests and linting'
	@echo '    make validate_python                       run Python unit tests and quality checks'
	@echo '    make fast_validate_python                  run Python unit tests (in parallel) and quality checks'
	@echo '    make quality                               run pycodestyle and Pylint'
	@echo '    make validate                              Run Python and JavaScript unit tests and linting'
	@echo '    make html_coverage                         generate and view HTML coverage report'
	@echo '    make e2e                                   run end to end acceptance tests'
	@echo '    make extract_translations                  extract strings to be translated'
	@echo '    make dummy_translations                    generate dummy translations'
	@echo '    make compile_translations                  generate translation files'
	@echo '    make fake_translations                     install fake translations'
	@echo '    make pull_translations                     pull translations from via atlas'
	@echo '    make update_translations                   install new translations from Transifex'
	@echo '    make clean_static                          delete compiled/compressed static assets'
	@echo '    make static                                compile and compress static assets'
	@echo '    make detect_changed_source_translations    check if translation files are up-to-date'
	@echo '    make check_translations_up_to_date         install fake translations and check if translation files are up-to-date'
	@echo '    make production-requirements               install requirements for production'
	@echo '    make validate_translations                 validate translations'
	@echo '    make check_keywords                        scan Django models in installed apps for restricted field names'
	@echo '    make docs                                  build the sphinx docs for this project'
	@echo ''

requirements.js:
	npm ci
	# Allow root for Docker
	$(NODE_BIN)/bower install --allow-root

requirements: requirements.js
	pip3 install -r requirements/pip_tools.txt
	pip3 install -r requirements/dev.txt --exists-action w

requirements.tox:
	pip3 install -U pip==20.0.2
	pip3 install -r requirements/tox.txt --exists-action w

production-requirements: requirements.js
	pip3 install -r requirements.txt --exists-action w

migrate: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-migrate

serve: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-serve

clean:
	find . -name '*.pyc' -delete
	rm -rf coverage htmlcov

clean_static:
	rm -rf assets/* ecommerce/static/build/*

run_check_isort: requirements.tox
	tox -e $(PYTHON_ENV)-check_isort

run_isort: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-run_isort

run_pycodestyle: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-pycodestyle

run_pep8: run_pycodestyle

run_pylint: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-pylint

quality: run_check_isort run_pycodestyle run_pylint

validate_js:
	rm -rf coverage
	$(NODE_BIN)/gulp test
	$(NODE_BIN)/gulp lint

validate_python: clean requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-tests

acceptance: clean requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-acceptance

fast_validate_python: clean requirements.tox
	DISABLE_ACCEPTANCE_TESTS=True tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-tests

validate: validate_python validate_js quality

theme_static: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-theme_static

static: requirements.js theme_static requirements.tox
	$(NODE_BIN)/r.js -o build.js
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-static

html_coverage: requirements.tox
	tox -e $(PYTHON_ENV)-coverage_html

diff_coverage: validate fast_diff_coverage

fast_diff_coverage: requirements.tox
	tox -e $(PYTHON_ENV)-fast_diff_coverage

e2e: requirements.tox
	tox -e $(PYTHON_ENV)-e2e

extract_translations: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-extract_translations

dummy_translations: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-dummy_translations

compile_translations: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-compile_translations

fake_translations: extract_translations dummy_translations compile_translations

pull_translations:
	find ecommerce/conf/locale -mindepth 1 -maxdepth 1 -type d -exec rm -r {} \;
	atlas pull $(ATLAS_OPTIONS) translations/ecommerce/ecommerce/conf/locale:ecommerce/conf/locale
	python manage.py compilemessages

update_translations: pull_translations fake_translations

# extract_translations should be called before this command can detect changes
detect_changed_source_translations: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-detect_changed_translations

# @FIXME: skip detect_changed_source_translations until git diff works again (REV-2737)
check_translations_up_to_date: fake_translations # detect_changed_source_translations

# Validate translations
validate_translations: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-validate_translations

# Scan the Django models in all installed apps in this project for restricted field names
check_keywords: requirements.tox
	tox -e $(PYTHON_ENV)-${DJANGO_ENV_VAR}-check_keywords

COMMON_CONSTRAINTS_TXT=requirements/common_constraints.txt
.PHONY: $(COMMON_CONSTRAINTS_TXT)
$(COMMON_CONSTRAINTS_TXT):
	wget -O "$(@)" https://raw.githubusercontent.com/edx/edx-lint/master/edx_lint/files/common_constraints.txt || touch "$(@)"

upgrade: export CUSTOM_COMPILE_COMMAND=make upgrade
upgrade: $(COMMON_CONSTRAINTS_TXT)
	pip install -q -r requirements/pip_tools.txt
	pip-compile --rebuild --upgrade --allow-unsafe -o requirements/pip.txt requirements/pip.in
	pip-compile --rebuild --upgrade -o requirements/pip_tools.txt requirements/pip_tools.in
	pip install -qr requirements/pip.txt
	pip install -qr requirements/pip_tools.txt
	pip-compile --upgrade -o requirements/tox.txt requirements/tox.in
	pip-compile --upgrade -o requirements/base.txt requirements/base.in
	pip-compile --upgrade -o requirements/docs.txt requirements/docs.in
	pip-compile --upgrade -o requirements/e2e.txt requirements/e2e.in
	pip-compile --upgrade -o requirements/test.txt requirements/test.in
	pip-compile --upgrade -o requirements/dev.txt requirements/dev.in
	pip-compile --upgrade -o requirements/production.txt requirements/production.in
	# Let tox control the Django version for tests
	sed '/^[dD]jango==/d' requirements/test.txt > requirements/test.tmp
	mv requirements/test.tmp requirements/test.txt

docs:
	tox -e docs

# Targets in a Makefile which do not produce an output file with the same name as the target name
.PHONY: help requirements migrate serve clean validate_python quality validate_js validate html_coverage e2e \
	extract_translations dummy_translations compile_translations fake_translations pull_translations \
	update_translations fast_validate_python clean_static production-requirements \
	docs
