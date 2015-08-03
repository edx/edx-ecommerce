module.exports = function(config) {
  config.set({

    // base path that will be used to resolve all patterns (eg. files, exclude)
    basePath: '',

    // list of files / patterns to load in the browser
	files: [
      {pattern: 'ecommerce/static/vendor/**/*.js', included: false},
      {pattern: 'ecommerce/static/bower_components/**/*.js', included: false},
      {pattern: 'ecommerce/static/js/models/**/*.js', included: false},
      {pattern: 'ecommerce/static/js/views/**/*.js', included: false},
      {pattern: 'ecommerce/static/js/collections/**/*.js', included: false},
      {pattern: 'ecommerce/static/js/test/specs/*.js', included: false},
      {pattern: 'ecommerce/static/templates/**/*.html', included: false},
      'ecommerce/static/js/config.js',
      'ecommerce/static/js/test/spec-runner.js'
    ],

    // list of files to exclude
    exclude: [
    ],

    // preprocess matching files before serving them to the browser
    // available preprocessors: https://npmjs.org/browse/keyword/karma-preprocessor
    preprocessors: {
        'ecommerce/static/js/views/**/*.js': ['coverage'],
        'ecommerce/static/js/models/**/*.js': ['coverage'],
        'ecommerce/static/js/collections/**/*.js': ['coverage']
    },

    // enabled plugins
    plugins:[
       'karma-jasmine',
       'karma-requirejs',
       'karma-jasmine-jquery',
       'karma-firefox-launcher',
       'karma-coverage',
       'karma-spec-reporter',
       'karma-sinon'
   ],

    // Karma coverage config
    coverageReporter: {
        type : 'text'
    },

    // test results reporter to use
    // possible values: 'dots', 'progress'
    // available reporters: https://npmjs.org/browse/keyword/karma-reporter
    reporters: ['spec', 'coverage'],

    // frameworks to use
    // available frameworks: https://npmjs.org/browse/keyword/karma-adapter
    frameworks: ['jasmine-jquery', 'jasmine', 'requirejs', 'sinon'],

    // web server port
    port: 9876,


    // enable / disable colors in the output (reporters and logs)
    colors: true,


    // level of logging
    // possible values: config.LOG_DISABLE || config.LOG_ERROR || config.LOG_WARN || config.LOG_INFO || config.LOG_DEBUG
    logLevel: config.LOG_INFO,


    // enable / disable watching file and executing tests whenever any file changes
    autoWatch: false,


    // start these browsers
    // available browser launchers: https://npmjs.org/browse/keyword/karma-launcher
    browsers: ['Firefox'],


    // Continuous Integration mode
    // if true, Karma captures browsers, runs the tests and exits
    singleRun: true
  })
}
