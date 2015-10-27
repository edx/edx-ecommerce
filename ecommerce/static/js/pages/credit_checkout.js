require([
        'jquery',
        'backbone',
        'models/user_model',
        'models/tracking_model',
        'models/course_model',
        'collections/credit_provider_collection',
        'views/clickable_view',
        'views/analytics_view',
        'views/payment_button_view',
        'utils/utils',
        'views/credit_provider_view'
    ],
    function ($,
              Backbone,
              UserModel,
              TrackingModel,
              CourseModel,
              CreditProviderCollection,
              ClickableView,
              AnalyticsView,
              PaymentButtonView,
              Utils,
              CreditProviderView) {
        'use strict';

        var $container = $('.credit-checkout'),
            $providerDetails = $container.find('.provider-details'),
            lmsRootUrl = $container.data('lms-root-url');

        new CreditProviderView({
            el: $providerDetails,
            collection: new CreditProviderCollection({
                lmsRootUrl: lmsRootUrl,
                providerIds: $providerDetails.data('provider-ids')
            })
        });

        new PaymentButtonView({
            el: $('#payment-buttons')
        });

        var courseModel = new CourseModel(),
            trackingModel = new TrackingModel(),
            userModel = new UserModel();

        /* jshint ignore:start */
        // initModelData is set by the Django template at render time.
        trackingModel.set(initModelData.tracking);
        userModel.set(initModelData.user);
        courseModel.set(initModelData.course);
        /* jshint ignore:end */

        /*
         Triggering the analytics events on clicking the payment buttons on checkoutpage.
         Buttons has the data-track- attributes type , event and category.
         */

        new AnalyticsView({
            model: trackingModel,
            userModel: userModel,
            courseModel: courseModel
        });

        // instrument the click events
        _($('[data-track-type="click"]')).each(function (track) {
            var properties = Utils.getNodeProperties(track.attributes,
                'data-track-', ['data-track-event']);

            new ClickableView({
                model: trackingModel,
                trackEventType: $(track).attr('data-track-event'),
                trackProperties: properties,
                el: track
            });
        });
    }
);
