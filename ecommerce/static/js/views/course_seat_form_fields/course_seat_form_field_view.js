define([
        'jquery',
        'backbone',
        'backbone-validation',
        'backbone.stickit',
        'underscore',
        'underscore.string'
    ],
    function ($,
              Backbone,
              BackboneValidation,
              BackboneStickit,
              _,
              _s) {
        'use strict';

        return Backbone.View.extend({
            seatType: null,
            template: null,

            bindings: {
                'input[name=certificate_type]': 'certificate_type',
                'input[name=price]': {
                    observe: 'price',
                    setOptions: {
                        validate: true
                    }
                },
                'input[name=expires]': 'expires',
                'input[name=id_verification_required]': {
                    observe: 'id_verification_required',
                    onSet: 'cleanIdVerificationRequired'
                }
            },

            className: function () {
                return 'row course-seat ' + this.seatType;
            },

            initialize: function () {
                Backbone.Validation.bind(this, {
                    valid: function (view, attr, selector) {
                        var $el = view.$('[name=' + attr + ']'),
                            $group = $el.closest('.form-group');

                        $group.removeClass('has-error');
                        $group.find('.help-block:first').html('').addClass('hidden');
                    },
                    invalid: function (view, attr, error, selector) {
                        var $el = view.$('[name=' + attr + ']'),
                            $group = $el.closest('.form-group');

                        $group.addClass('has-error');
                        $group.find('.help-block:first').html(error).removeClass('hidden');
                    }
                });
            },

            render: function () {
                this.$el.html(this.template(this.model.attributes));
                this.stickit();
                return this;
            },

            cleanIdVerificationRequired: function(val){
                return _s.toBoolean(val);
            },

            getFieldValue: function (name) {
                return this.$(_s.sprintf('input[name=%s]', name)).val();
            },

            // TODO Validate the input: http://thedersen.com/projects/backbone-validation/.

            /***
             * Return the input data from the form fields.
             */
            getData: function () {
                var data = {},
                    fields = ['certificate_type', 'id_verification_required', 'price', 'expires'];

                _.each(fields, function (field) {
                    data[field] = this.getFieldValue(field);
                }, this);

                return data;
            },

            updateModel: function () {
                this.model.set(this.getData());
            }
        });
    }
);
