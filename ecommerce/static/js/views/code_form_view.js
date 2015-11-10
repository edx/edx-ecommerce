// jscs:disable requireCapitalizedConstructors

define([
        'jquery',
        'backbone',
        'backbone.super',
        'backbone.validation',
        'backbone.stickit',
        'moment',
        'underscore',
        'underscore.string',
        'text!templates/code_form.html',
        'views/alert_view'
    ],
    function ($,
              Backbone,
              BackboneSuper,
              BackboneValidation,
              BackboneStickit,
              moment,
              _,
              _s,
              CodeFormTemplate,
              AlertView) {
        'use strict';

        // Extend the callbacks to work with Bootstrap.
        // See: http://thedersen.com/projects/backbone-validation/#callbacks
        _.extend(Backbone.Validation.callbacks, {
            valid: function (view, attr) {
                var $el = view.$('[name=' + attr + ']'),
                    $group = $el.closest('.form-group');

                $group.removeClass('has-error');
                $group.find('.help-block:first').html('').addClass('hidden');
            },
            invalid: function (view, attr, error) {
                var $el = view.$('[name=' + attr + ']'),
                    $group = $el.closest('.form-group');

                $group.addClass('has-error');
                $group.find('.help-block:first').html(error).removeClass('hidden');
            }
        });

        return Backbone.View.extend({
            tagName: 'form',

            className: 'code-form-view',

            template: _.template(CodeFormTemplate),

            events: {
                'submit': 'submit'
            },

            bindings: {

            },

            initialize: function (options) {
                this.alertViews = [];
                this.editing = options.editing || false;

                // Enable validation
                Backbone.Validation.bind(this);
            },

            remove: function () {
                Backbone.Validation.unbind(this);

                this.clearAlerts();

                _.each(this.courseSeatViews, function (view) {
                    view.remove();
                }, this);

                this.courseSeatViews = {};

                return this._super();
            },

            render: function () {
                // Render the parent form/template
                this.$el.html(this.template(this.model.attributes));

                this.stickit();

                // Avoid the need to create this jQuery object every time an alert has to be rendered.
                this.$alerts = this.$el.find('.alerts');

                return this;
            },

            /**
             * Renders alerts that will appear at the top of the page.
             *
             * @param {String} level - Severity of the alert. This should be one of success, info, warning, or danger.
             * @param {Sring} message - Message to display to the user.
             */
            renderAlert: function (level, message) {
                var view = new AlertView({level: level, title: gettext('Error!'), message: message});

                view.render();
                this.$alerts.append(view.el);
                this.alertViews.push(view);

                $('body').animate({
                    scrollTop: this.$alerts.offset().top
                }, 500);

                this.$alerts.focus();

                return this;
            },

            /**
             * Remove all alerts currently on display.
             */
            clearAlerts: function () {
                _.each(this.alertViews, function (view) {
                    view.remove();
                });

                this.alertViews = [];

                return this;
            },

            /**
             * Returns the value of an input field.
             *
             * @param {String} name - Name of the field whose value should be returned
             * @returns {*} - Value of the field.
             */
            getFieldValue: function (name) {
                return this.$(_s.sprintf('input[name=%s]', name)).val();
            },

            /**
             * Submits the form data to the server.
             *
             * If client-side validation fails, data will NOT be submitted. Server-side errors will result in an
             * alert being rendered. If submission succeeds, the user will be redirected to the course detail page.
             *
             * @param e
             */
            submit: function (e) {
                var $buttons,
                    $submitButton,
                    btnDefaultText,
                    self = this,
                    btnSavingContent = '<i class="fa fa-spinner fa-spin" aria-hidden="true"></i> ' +
                        gettext('Saving...');

                e.preventDefault();

                // Validate the input and display a message, if necessary.
                if (!this.model.isValid(true)) {
                    this.clearAlerts();
                    this.renderAlert('danger', gettext('Please complete all required fields.'));
                    return;
                }

                $buttons = this.$el.find('.form-actions .btn');
                $submitButton = $buttons.filter('button[type=submit]');

                // Store the default button text, and replace it with the saving state content.
                btnDefaultText = $submitButton.text();
                $submitButton.html(btnSavingContent);

                // Disable all buttons by setting the attribute (for <button>) and class (for <a>)
                $buttons.attr('disabled', 'disabled').addClass('disabled');

                this.model.save({
                    complete: function () {
                        // Restore the button text
                        $submitButton.text(btnDefaultText);

                        // Re-enable the buttons
                        $buttons.removeAttr('disabled').removeClass('disabled');
                    },
                    success: function (model) {
                        self.goTo(model.id);
                    },
                    error: function (model, response) {
                        var message = gettext('An error occurred while saving the data.');

                        if (response.responseJSON && response.responseJSON.error) {
                            message = response.responseJSON.error;

                            // Log the error to the console for debugging purposes
                            console.error(message);
                        } else {
                            // Log the error to the console for debugging purposes
                            console.error(response.responseText);
                        }

                        self.clearAlerts();
                        self.renderAlert('danger', message);
                        self.$el.animate({scrollTop: 0}, 'slow');
                    }
                });

                return this;
            }
        });
    }
);
