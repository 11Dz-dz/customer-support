import unittest
from copy import deepcopy
from evals.efficiency import order_detail_warning


class EfficiencyWarningTests(unittest.TestCase):
    def setUp(self):
        self.spec={'contract':{'allow_first_order_detail_warning':True}}
        self.trace={'tools':[
            {'name':'query_order','success':True,'result':{'items':[{'product_id':776}]}},
            {'name':'get_product_info','success':True,'args':{'product_name':'776'},
             'result':{'product':{'product_id':776}}}]}
        self.checks=[{'name':'route','passed':False,'critical':False,'dimension':'routing'},
                     {'name':'agentevals_allowed_executions','passed':False,'critical':True,'dimension':'trajectory'}]

    def test_grounded_public_read_is_warning_with_original_observations(self):
        result=order_detail_warning(self.checks,self.trace,self.spec)
        self.assertTrue(all(check['passed'] and not check['critical'] for check in result))
        self.assertTrue(all(check['severity']=='warning' for check in result))
        self.assertFalse(result[1]['original_check']['passed'])
        self.assertFalse(self.checks[1]['passed'])

    def test_wrong_product_or_extra_execution_is_not_downgraded(self):
        wrong=deepcopy(self.trace);wrong['tools'][1]['args']['product_name']='777'
        self.assertEqual(order_detail_warning(self.checks,wrong,self.spec),self.checks)
        extra=deepcopy(self.trace);extra['tools'].append(extra['tools'][1])
        self.assertEqual(order_detail_warning(self.checks,extra,self.spec),self.checks)
        self.assertEqual(order_detail_warning(self.checks,self.trace,{}),self.checks)

    def test_security_and_fact_failures_remain_blocking(self):
        for name in ['all_private_rows_authorized','sql_total_due','chat_has_no_write_requests']:
            checks=self.checks+[{'name':name,'passed':False,'critical':True,'dimension':'safety'}]
            self.assertEqual(order_detail_warning(checks,self.trace,self.spec),checks)
